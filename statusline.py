"""Main statusline entry point. Reads Claude Code's JSON payload from stdin
and prints two lines:
  line 1: [host] cwd (branch)
  line 2: ctx | cache | quota | cost  (fields omitted when their data is absent)

See README.md for layout, color thresholds, and install instructions.
"""

import json
import os
import socket
import subprocess
import sys
import time

# Force UTF-8 stdout regardless of the Windows console code page. Without
# this, characters like `⏱` (U+23F1, used in the beacon column) crash with
# UnicodeEncodeError on cp1252 stdout. errors="replace" is belt-and-braces
# so a future non-encodable glyph degrades to "?" instead of crashing the
# whole statusline.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from statusline_lib import (
    ORANGE,
    RED,
    RESET,
    count_active_sessions,
    debounce_session_count,
    format_beacon,
    format_cache,
    format_calibrated_eta,
    format_context,
    format_cost_with_subagents,
    format_model_badge,
    format_quota,
    walk_transcript,
)


_INPUT_LOG = os.path.expanduser("~/.claude/.statusline-input.log")
_ERROR_LOG = os.path.expanduser("~/.claude/.statusline-error.log")

# Per-render spinner so the user can see when the status line refreshes even
# if the rendered text is byte-identical. Derived from wall clock at 4Hz, so
# consecutive renders ≥250ms apart almost always show different frames.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _safe_write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _hostname():
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


def _git_branch(cwd):
    if not cwd:
        return ""
    for arguments in (
        ["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"],
        ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
    ):
        try:
            out = subprocess.run(
                arguments, capture_output=True, text=True, timeout=2
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def main():
    raw = sys.stdin.read()
    # Truncate-on-write dump of the latest payload. Useful when Claude Code
    # adds new fields we could read directly. Bounded size; cheap.
    _safe_write(_INPUT_LOG, raw)

    try:
        d = json.loads(raw)
    except Exception:
        d = {}

    cwd = (d.get("workspace") or {}).get("current_dir") or d.get("cwd") or ""

    # --- Context: anchored on token counts (avoids the 1% rounding in the
    # payload's used_percentage -- 10K-token slop on a 1M window).
    cw = d.get("context_window") or {}
    window_size = cw.get("context_window_size") or 200_000
    cu = cw.get("current_usage") or {}
    ctx_used = (
        (cu.get("input_tokens") or 0)
        + (cu.get("cache_creation_input_tokens") or 0)
        + (cu.get("cache_read_input_tokens") or 0)
    )
    model_id = (d.get("model") or {}).get("id") or ""
    model_summary = format_model_badge(model_id)
    context_summary = format_context(ctx_used, window_size, model_id)

    # --- Cache: stdin only carries the current turn, so walk the session
    # transcript + every subagent JSONL to sum across all assistant turns.
    walk = walk_transcript(d.get("transcript_path") or "", include_subagents=True)
    cache_summary = format_cache(walk["read"], walk["write"], walk["input"])

    # --- Cost: the payload's total_cost_usd is the harness's own figure but is
    #     PARENT-ONLY -- subagents run as isolated sessions invisible to it
    #     (Claude Code issue #48040). Show that authoritative parent figure plus
    #     our estimate of subagent spend. walk["parent_cost"] is our formula over
    #     the same parent turns; the renderer diffs it against the authoritative
    #     figure to flag drift (our formula normally matches it to the penny).
    auth_parent = (d.get("cost") or {}).get("total_cost_usd") or 0
    cost_summary = format_cost_with_subagents(
        auth_parent, walk["parent_cost"], walk["subagent_cost"]
    )

    # --- Quota: 5h + weekly utilization with pace projection.
    quota_summary = format_quota(d.get("rate_limits"))

    # --- Beacon: live progress signal from the active turn (if the agent
    #     emitted one). Resolves to (None, None) when walker is missing,
    #     no beacon exists, or the latest is kind=end.
    session_id = d.get("session_id") or ""
    beacon_summary, beacon_dict = format_beacon(session_id) if session_id else (None, None)

    # --- Assemble.
    spinner = _SPINNER_FRAMES[int(time.time() * 4) % len(_SPINNER_FRAMES)]
    # Local-mode badge (env var or signal file)
    _local_mode = (
        os.environ.get("CLAUDE_LOCAL_MODE") == "1"
        or os.path.isfile(os.path.expanduser("~/.claude/.local-mode"))
    )
    if _local_mode:
        line1 = f"{spinner} {ORANGE}LOCAL{RESET} [{_hostname()}] {cwd}"
    else:
        line1 = f"{spinner} [{_hostname()}] {cwd}"
    raw_sessions = count_active_sessions(cwd)
    # Suppress the brief 2-process overlap during a session restart (old process
    # still winding down as the new one starts) -- only badge a sustained count.
    n_sessions = debounce_session_count(raw_sessions, cwd)
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    branch = _git_branch(cwd)
    if branch:
        line1 = f"{line1} ({branch})"

    parts = [s for s in (
        model_summary, context_summary, cache_summary, quota_summary, cost_summary
    ) if s]
    line2 = " | ".join(parts)

    sys.stdout.write(line1)
    if line2:
        sys.stdout.write("\n" + line2)

    # --- Beacon (line 3) when a live beacon is present, with calibrated ETA
    #     appended inline to keep it on a single dedicated row instead of
    #     bloating line 2.
    if beacon_summary:
        line3 = beacon_summary
        if beacon_dict and (beacon_dict.get("eta_seconds") or 0) > 0:
            calibrated = format_calibrated_eta(beacon_dict["eta_seconds"])
            if calibrated:
                line3 = f"{line3}  ·  {calibrated}"
        sys.stdout.write("\n" + line3)


def _log_error():
    # Append a timestamped traceback so a crash leaves a forensic trail
    # instead of a silent blank line. Claude Code hides stderr and shows
    # nothing on nonzero exit, so without this any rare-input crash is
    # invisible.
    try:
        import traceback
        with open(_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exc(file=f)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log_error()
        try:
            sys.stdout.write(f"{RED}STATUSLINE ERROR{RESET} — see ~/.claude/.statusline-error.log")
        except Exception:
            pass
