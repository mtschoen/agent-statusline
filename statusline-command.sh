#!/usr/bin/env bash
# Claude Code status line.  All data comes from the JSON payload Claude Code
# pipes on stdin (Claude Code v2.1+); no transcript parsing, no API calls.
# Layout:
#   line 1: [host] cwd (branch)
#   line 2: ctx | cache | 5h | wk | cost
# Color thresholds:
#   ctx        green <50  yellow 50-60  red >=60   (auto-compact at ~66%)
#   5h, wk     green <75  yellow 75-90  red >=90
#   cache hit  green >=90 yellow 75-90  red <75    (high-is-good)
#   cost       green <$25 yellow $25-$50 red >=$50
# Projection (5h, wk): "+/-X.Yh" green when >5% margin to window reset,
# yellow when 0-5% margin (close call), red when projecting to hit the cap.

input=$(cat)
# Truncate-on-write dump of the latest stdin payload.  Useful when Claude Code
# adds new fields (e.g. previously gained `rate_limits`, `context_window`)
# we could read directly.  Bounded size; cheap.
printf '%s' "$input" > "$HOME/.claude/.statusline-input.log"

host=$(hostname -s 2>/dev/null || hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo "unknown")

if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

# Single Python call: parse stdin, build summary fields, emit tab-separated.
# Caveat: this whole block lives inside a bash single-quoted heredoc, so a
# single quote ANYWHERE in the Python source closes the heredoc and breaks
# the script.  Use double quotes everywhere; extract values to vars before
# f-stringing them.
parsed=$(PYTHONIOENCODING=utf-8 "$PY" -c '
import sys, json, os, glob
from datetime import datetime, timezone

try:
    d = json.load(sys.stdin)
except Exception:
    d = {}

cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or ""

RED = "\x1b[31m"
YELLOW = "\x1b[33m"
GREEN = "\x1b[32m"
RESET = "\x1b[0m"

def fmt(n):
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1000:.1f}K"
    return str(n)

# higher-is-worse: ctx, 5h, wk (>= warn -> yellow, >= danger -> red)
def _color_high_bad(pct, warn, danger, decimals=0):
    c = RED if pct >= danger else YELLOW if pct >= warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"

# higher-is-better: cache hit (< warn -> yellow, < danger -> red)
def _color_high_good(pct, warn, danger, decimals=0):
    c = RED if pct < danger else YELLOW if pct < warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"

def _fmt_delta(seconds):
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds) / 3600:.1f}h"

# Returns colored +/-X.Yh string (or empty if not enough data). Period in seconds.
def _project(util, resets_at_unix, period_seconds):
    if util is None or util <= 0 or not resets_at_unix:
        return ""
    try:
        reset_dt = datetime.fromtimestamp(resets_at_unix, tz=timezone.utc)
        remaining = (reset_dt - datetime.now(timezone.utc)).total_seconds()
        elapsed = period_seconds - remaining
        if elapsed <= 0 or remaining <= 0:
            return ""
        delta = 100.0 * elapsed / util - period_seconds
        warn_threshold = 0.05 * period_seconds
        if delta < 0:
            color = RED
        elif delta <= warn_threshold:
            color = YELLOW
        else:
            color = GREEN
        return f" {color}{_fmt_delta(delta)}{RESET}"
    except Exception:
        return ""

# --- Context ---------------------------------------------------------------
cw = d.get("context_window") or {}
window_size = cw.get("context_window_size") or 200_000
ctx_pct = cw.get("used_percentage") or 0
cu = cw.get("current_usage") or {}
ctx_used = (cu.get("input_tokens") or 0) + (cu.get("cache_creation_input_tokens") or 0) + (cu.get("cache_read_input_tokens") or 0)
context_summary = f"ctx: {fmt(ctx_used)} / {fmt(window_size)} ({_color_high_bad(ctx_pct, 50, 60, 1)})"

# --- Cache hit (session-wide — stdin JSON only exposes current turn, so we
# still walk the session transcript + every subagent JSONL to sum across all
# assistant turns).  Cost & ctx % come from stdin and do NOT need this walk.
def _sum_cache_tokens(transcript_path):
    read_total = write_total = input_total = 0
    seen = set()
    def process(p):
        nonlocal read_total, write_total, input_total
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    msg = e.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    mid = msg.get("id")
                    if mid:
                        # transcripts can repeat the same assistant turn under
                        # one message.id (snapshots/checkpoints carry the same
                        # usage); count each id once.
                        if mid in seen:
                            continue
                        seen.add(mid)
                    u = msg.get("usage") or {}
                    read_total += int(u.get("cache_read_input_tokens") or 0)
                    write_total += int(u.get("cache_creation_input_tokens") or 0)
                    input_total += int(u.get("input_tokens") or 0)
        except Exception:
            pass
    if transcript_path and os.path.exists(transcript_path):
        process(transcript_path)
        if transcript_path.endswith(".jsonl"):
            sub_dir = transcript_path[:-6] + "/subagents"
            if os.path.isdir(sub_dir):
                for sub in glob.glob(os.path.join(sub_dir, "agent-*.jsonl")):
                    process(sub)
    return read_total, write_total, input_total

read_t, write_t, input_t = _sum_cache_tokens(d.get("transcript_path") or "")
total_in = read_t + write_t + input_t
cache_summary = ""
if total_in > 0:
    hit_pct = read_t * 100 / total_in
    cache_summary = f"cache: {fmt(read_t)} read / {fmt(write_t)} write / {_color_high_good(hit_pct, 90, 75)} hit"

# --- Cost ------------------------------------------------------------------
cost = (d.get("cost") or {}).get("total_cost_usd") or 0
if cost > 0:
    cost_color = RED if cost >= 50 else YELLOW if cost >= 25 else GREEN
    cost_summary = f"cost: {cost_color}${cost:.2f}{RESET}"
else:
    cost_summary = ""

# --- Rate limits (5h, weekly) ----------------------------------------------
rl = d.get("rate_limits") or {}
usage_parts = []
for win_key, period_seconds, label in (("five_hour", 5 * 3600, "5h"), ("seven_day", 7 * 86400, "wk")):
    w = rl.get(win_key) or {}
    util = w.get("used_percentage")
    if util is None:
        continue
    resets_at = w.get("resets_at")
    pct_part = _color_high_bad(util, 75, 90)
    proj_part = _project(util, resets_at, period_seconds)
    usage_parts.append(f"{label}: {pct_part}{proj_part}")
usage_summary = " ".join(usage_parts)

sys.stdout.write(cwd + "\t" + context_summary + "\t" + cache_summary + "\t" + usage_summary + "\t" + cost_summary)
' <<<"$input" 2>"$HOME/.claude/.statusline-error.log")

IFS=$'\t' read -r cwd context_summary cache_summary usage_summary cost_summary <<<"$parsed"

branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null \
  || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)

# Line 1: location.  Line 2: metrics — joined with " | ", omitted entirely when empty.
line1="[$host] $cwd"
[ -n "$branch" ] && line1="$line1 ($branch)"

line2=""
sep=""
for field in "$context_summary" "$cache_summary" "$usage_summary" "$cost_summary"; do
  if [ -n "$field" ]; then
    line2="${line2}${sep}${field}"
    sep=" | "
  fi
done

printf "%s" "$line1"
[ -n "$line2" ] && printf "\n%s" "$line2"
