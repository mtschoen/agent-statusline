"""Main statusline entry point. Reads Claude Code's JSON payload from stdin
and prints up to three lines:
  line 1: [host] home [rel-cwd] (branch) <session title, if it fits>
  line 2: ctx | cache | ttl | quota | cost | +/-lines  (fields omitted when their data is absent)
  line 3: session wall/api timing  ·  weekly-quota exhaustion clock (>90%)  ·  live turn beacon + calibrated ETA

See README.md for layout, color thresholds, and install instructions.
"""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from typing import NamedTuple

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
    app_dir,
    count_active_sessions,
    debounce_session_count,
    format_beacon,
    format_burn_rate,
    format_cache,
    format_calibrated_eta,
    format_context,
    format_cost_with_subagents,
    format_day_budget,
    format_lines,
    format_model_badge,
    format_quota,
    format_session_timing,
    format_ttl,
    pref_bool,
    resolve_flags,
    terminal_columns,
    visible_width,
    walk_transcript,
    weekly_exhaustion,
)
from statusline_lib.nudge import write_ctx_state

_INPUT_LOG = os.path.join(app_dir(), ".statusline-input.log")
_ERROR_LOG = os.path.join(app_dir(), ".statusline-error.log")

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _safe_write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        # Best-effort write (cache/state file); a failed write is non-fatal
        # and must not break rendering.
        pass


def _hostname():
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


def _git_command(cwd, *arguments):
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *arguments],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


_GIT_HASH_COLOR = "\x1b[38;5;137m"  # muted tan - distinct from the blue session badge
_HOST_COLOR = "\x1b[38;5;96m"  # muted mauve - distinct from the tan hash and blue badge


def _git_ref(cwd):
    """Render the git ref as `branch:hash` (e.g. `main:abc123`) so the commit
    hash is visually distinct from the session-id badge on line 1. The hash is
    tinted a muted tan while the branch keeps the default colour. On a detached
    HEAD there is no branch, so just the short hash is shown."""
    if not cwd:
        return ""
    branch = _git_command(cwd, "symbolic-ref", "--short", "HEAD")
    short_hash = _git_command(cwd, "rev-parse", "--short", "HEAD")
    tinted_hash = f"{_GIT_HASH_COLOR}{short_hash}{RESET}" if short_hash else ""
    if branch and tinted_hash:
        return f"{branch}:{tinted_hash}"
    return branch or tinted_hash


# Desaturated teal (256-color 66, #5f8787): when the session has cd'd away
# from its launch dir, the relative hop is rendered in this muted teal so the
# fixed "home" stays the visual anchor and the move reads as secondary.
_CWD_REL_COLOR = "\x1b[38;5;66m"


def _format_cwd(home, current):
    """Render the session's launch dir as the stable anchor, appending the
    current working dir as a desaturated-teal relative hop when it has moved.

    Claude Code's payload carries both: workspace.project_dir is fixed at
    launch while workspace.current_dir follows shell `cd`. Anchoring on home
    keeps the statusline readable even after the session wanders.
    """
    if not home:
        return current
    if not current or os.path.normcase(os.path.normpath(home)) == os.path.normcase(
        os.path.normpath(current)
    ):
        return home
    try:
        relative = os.path.relpath(current, home)
    except ValueError:
        # Different drive on Windows: no relative path exists.
        relative = None
    # A leading ".." means the session has stepped out above home; a relative
    # path there is more confusing than helpful, so show the absolute dir.
    # Nested moves get a leading "./" (os.sep keeps it native: ".\" on Windows,
    # "./" on POSIX) so the hop reads unambiguously as relative-to-home.
    if relative is None or relative.startswith(".."):
        hop = current
    else:
        hop = f".{os.sep}{relative}"
    return f"{home} {_CWD_REL_COLOR}[{hop}]{RESET}"


def _line1(d, cwd, cwd_display, spinner):
    local_mode = (
        os.environ.get("CLAUDE_LOCAL_MODE") == "1"
        or os.environ.get("ANTIGRAVITY_LOCAL_MODE") == "1"
        or os.path.isfile(os.path.join(app_dir(), ".local-mode"))
    )
    host = f"{_HOST_COLOR}{_hostname()}{RESET}"
    line1 = (
        f"{spinner} {ORANGE}LOCAL{RESET} [{host}] {cwd_display}"
        if local_mode
        else f"{spinner} [{host}] {cwd_display}"
    )
    # Suppress the brief 2-process overlap during a session restart (old process
    # still winding down as the new one starts) -- only badge a sustained count.
    n_sessions = debounce_session_count(count_active_sessions(cwd), cwd)
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    ref = _git_ref(cwd)
    if ref:
        line1 = f"{line1} ({ref})"
    session_id = d.get("session_id") or d.get("conversation_id")
    session_name = d.get("session_name") or d.get("session_title") or d.get("title")
    line1 = _append_session_id(line1, session_id)
    return _append_session_name(line1, session_name)


# Muted grey so the session title reads as a secondary label, not a headline.
_SESSION_NAME_COLOR = "\x1b[38;5;245m"
_SESSION_NAME_MAX = 58
# Shorten the session UUID to its first hex group - enough to disambiguate
# concurrent sessions without eating line-1 width.
_SESSION_ID_COLOR = "\x1b[38;5;67m"  # muted steel blue
_SESSION_ID_LEN = 8


def _append_session_id(line1, session_id):
    """Append a short session-id hash in brackets after the path/branch.
    Unconditional - it is tiny and useful for matching a statusline to a
    transcript file, so unlike the session title it is not width-gated."""
    sid = (session_id or "").strip()
    if not sid:
        return line1
    return f"{line1} {_SESSION_ID_COLOR}[{sid[:_SESSION_ID_LEN]}]{RESET}"


def _append_session_name(line1, session_name):
    """Append the auto-generated session title after the path/branch, but only
    when it fits. Width comes from `$COLUMNS` (the same source line 2 uses); if
    that is unset (older Claude Code) we append best-effort. The title is the
    first thing to yield - it is a nicety, never worth pushing the path off
    screen - so on a known-too-narrow terminal it is dropped entirely. Long
    titles are clipped to keep line 1 bounded even when width is unknown."""
    name = (session_name or "").strip()
    if not name:
        return line1
    if len(name) > _SESSION_NAME_MAX:
        name = name[: _SESSION_NAME_MAX - 1] + "…"
    segment = f" {_SESSION_NAME_COLOR}{name}{RESET}"
    cols = terminal_columns()
    if cols is not None and visible_width(line1) + visible_width(segment) > cols:
        return line1
    return f"{line1}{segment}"


def _beacon_line(session_id):
    # STATUSLINE_BEACON (default on) gates the whole beacon row -- the live
    # `⏱ turn ...` column AND the calibrated-ETA tail. It only suppresses
    # RENDERING; the agent still emits <progress-beacon> blocks into the
    # transcript, so flipping it back on resumes mid-lifecycle.
    if not pref_bool("STATUSLINE_BEACON", default=True):
        return None
    beacon_summary, beacon_dict = (
        format_beacon(session_id) if session_id else (None, None)
    )
    if not beacon_summary:
        return None
    if beacon_dict and (beacon_dict.get("eta_seconds") or 0) > 0:
        calibrated = format_calibrated_eta(beacon_dict["eta_seconds"])
        if calibrated:
            return f"{beacon_summary}  ·  {calibrated}"
    return beacon_summary


def _hide_cost():
    """STATUSLINE_HIDE_COST truthy -> suppress every dollar figure on line 2.

    Accepts 1/true/on/yes (any case). Anything else, including unset, shows
    money as before. A deliberate calm switch: quota %/time-to-limit (the
    non-dollar runway signal) stays, so you keep the useful budgeting info
    without a session-cost figure attached to a run you might have to discard.
    """
    return pref_bool("STATUSLINE_HIDE_COST", default=False)


class _Line2(NamedTuple):
    """Pre-computed inputs to line 2's compact re-render. Context is carried raw
    (not pre-rendered) so the compact resolver can drop its denominator and
    percentage; the cheap format_context call re-runs per flag set."""

    model_summary: str
    ctx_used: int
    window_size: int
    model_id: str
    walk: dict
    rate_limits: dict | None
    day_budget_summary: str
    cost_summary: str
    # STATUSLINE_HIDE_COST: when True, every dollar-denominated figure is
    # suppressed (session cost, $/min burn + target, day budget, the cache $
    # parens, the TTL wasted-$ estimate). Token counts, hit%, the TTL eviction
    # COUNT, context, and quota %/time-to-limit all stay - none of those carry a $.
    hide_cost: bool
    # Pre-rendered `+A/-B` session diffstat. Not money, so it is NOT gated by
    # hide_cost - only by its own `lines` compact-drop flag.
    lines_summary: str


def _render_line2(flags, inputs):
    """Format line 2 at the verbosity given by `flags` (the compact resolver
    flips entries off to fit $COLUMNS). `inputs` carries the already-computed,
    flag-independent summaries plus the raw walk/rate_limits; only the cheap
    formatting re-runs per flag set."""
    walk = inputs.walk
    # The money master switch ANDs into every dollar-bearing flag below, so it
    # overrides regardless of width: hidden money never reappears just because
    # the terminal is wide enough to show it.
    money = not inputs.hide_cost
    context_summary = format_context(
        inputs.ctx_used,
        inputs.window_size,
        inputs.model_id,
        show_denom=flags["context_denom"],
        show_pct=flags["context_pct"],
    )
    cache_summary = format_cache(
        walk["read"],
        walk["write"],
        walk["input"],
        walk["read_cost"],
        walk["write_cost"],
        show_costs=flags["cache_costs"] and money,
        show_hit=flags["cache_hit"],
        output_t=walk["output"],
        input_cost=walk["input_cost"],
        output_cost=walk["output_cost"],
        show_input=flags["cache_input"] and money,
        show_output=flags["cache_output"] and money,
    )
    ttl_summary = format_ttl(
        walk["ttl_evictions"],
        walk["ttl_wasted"],
        show_wasted=flags["ttl_wasted"] and money,
    )
    quota_summary = format_quota(inputs.rate_limits, show_pace=flags["quota_pace"])
    burnrate_summary = (
        format_burn_rate(inputs.rate_limits, show_target=flags["burn_target"])
        if flags["burn_rate"] and money
        else ""
    )
    parts = [
        s
        for s in (
            inputs.model_summary,
            context_summary,
            cache_summary,
            ttl_summary,
            quota_summary,
            inputs.day_budget_summary if money else "",
            burnrate_summary,
            inputs.cost_summary if money else "",
            inputs.lines_summary if flags["lines"] else "",
        )
        if s
    ]
    return " | ".join(parts)


def main():
    raw = sys.stdin.read()
    # Truncate-on-write dump of the latest payload. Useful when Claude Code
    # adds new fields we could read directly. Bounded size; cheap.
    _safe_write(_INPUT_LOG, raw)

    try:
        d = json.loads(raw)
    except Exception:
        d = {}

    workspace = d.get("workspace") or {}
    # current_dir follows shell `cd`; project_dir is the fixed launch dir.
    cwd = workspace.get("current_dir") or d.get("cwd") or ""
    cwd_display = _format_cwd(workspace.get("project_dir") or "", cwd)

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
    model_obj = d.get("model") or {}
    model_id = model_obj.get("id") or ""
    model_summary = format_model_badge(model_id, model_obj.get("display_name") or "")

    session_id = d.get("session_id") or d.get("conversation_id") or ""

    # Bridge occupancy to the wrap nudge hook (its payload can't see it).
    write_ctx_state(session_id, ctx_used, window_size, time.time())

    # Walk the session + subagent JSONLs to sum cache/cost/TTL across all turns.
    transcript_path = d.get("transcript_path")
    if not transcript_path and session_id:
        from statusline_lib.beacon import _find_session_jsonl

        transcript_path = _find_session_jsonl(session_id)
    walk = walk_transcript(transcript_path or "", include_subagents=True)

    # Payload total_cost_usd is parent-only (Claude Code issue #48040: subagents
    # are isolated sessions). Pair it with our subagent estimate; walk["parent_cost"]
    # lets us flag drift.
    cost = d.get("cost") or {}
    auth_parent = cost.get("total_cost_usd") or 0
    cost_summary = format_cost_with_subagents(
        auth_parent, walk["parent_cost"], walk["subagent_cost"]
    )
    # Session diffstat (+A/-B) straight from the payload; not money, so it shows
    # even under STATUSLINE_HIDE_COST.
    lines_summary = format_lines(
        cost.get("total_lines_added"), cost.get("total_lines_removed")
    )

    # Daily budget is flag-independent; compute once outside the compact loop.
    rate_limits = d.get("rate_limits")
    day_budget_summary = format_day_budget(rate_limits)

    spinner = _SPINNER_FRAMES[int(time.time() * 4) % len(_SPINNER_FRAMES)]
    line1 = _line1(d, cwd, cwd_display, spinner)

    # Resolve compact verbosity (STATUSLINE_COMPACT + $COLUMNS): re-render the
    # already-walked data at each flag set until it fits, then render once more.
    line2_inputs = _Line2(
        model_summary,
        ctx_used,
        window_size,
        model_id,
        walk,
        rate_limits,
        day_budget_summary,
        cost_summary,
        _hide_cost(),
        lines_summary,
    )
    flags = resolve_flags(lambda f: _render_line2(f, line2_inputs))
    line2 = _render_line2(flags, line2_inputs)

    sys.stdout.write(line1)
    if line2:
        sys.stdout.write("\n" + line2)

    # Line 3: session wall/api timing (always available), then the weekly-quota
    # exhaustion clock (only past 90% and projected to run out before reset),
    # then the live turn beacon + calibrated ETA (only while a turn is in
    # flight). Any may be absent; join with the same separator the beacon uses.
    line3 = "  ·  ".join(
        part
        for part in (
            format_session_timing(cost),
            weekly_exhaustion(rate_limits),
            _beacon_line(session_id),
        )
        if part
    )
    if line3:
        sys.stdout.write("\n" + line3)


def _log_error():
    try:
        import traceback

        with open(_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exc(file=f)
    except OSError:
        # The error logger itself must never raise; if the log file is
        # unwritable there is nothing useful left to do.
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log_error()
        with contextlib.suppress(Exception):
            log_path = os.path.join(app_dir(), ".statusline-error.log")
            readable_log_path = log_path.replace(os.path.expanduser("~"), "~").replace(
                "\\", "/"
            )
            sys.stdout.write(f"{RED}STATUSLINE ERROR{RESET} — see {readable_log_path}")
