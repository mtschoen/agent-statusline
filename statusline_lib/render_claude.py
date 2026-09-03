"""Claude Code and Antigravity CLI status-line render: line 1 (host, cwd,
git ref, session-id badge, turn count, session title), line 2 (the
compact-mode orchestration around render_line2), and line 3 (session
timing, weekly-quota exhaustion, teammates, live beacon, previous-render
suffix).

Extracted from statusline.py so the resident server can render this
harness without importing the entry script. See statusline.py's module
docstring for the overall layout this produces.
"""

import os

from statusline_lib import (
    ORANGE,
    RED,
    RESET,
    app_dir,
    count_active_sessions,
    debounce_session_count,
    format_agent_state,
    format_beacon,
    format_calibrated_eta,
    format_cost_with_subagents,
    format_day_budget,
    format_lines,
    format_model_badge,
    format_session_timing,
    format_teammates,
    format_turn_count,
    hostname,
    is_local_mode,
    pref_bool,
    resolve_flags,
    spinner_frame,
    terminal_columns,
    visible_width,
    weekly_exhaustion,
)
from statusline_lib.beacon import _find_session_jsonl
from statusline_lib.gitref import _git_ref_raw_cached
from statusline_lib.render_line2 import Line2Inputs, hide_cost, render_line2
from statusline_lib.rendertimer import format_render_suffix

_GIT_HASH_COLOR = "\x1b[38;5;137m"  # muted tan - distinct from the blue session badge
_HOST_COLOR = "\x1b[38;5;96m"  # muted mauve - distinct from the tan hash and blue badge


def _git_ref(cwd, state_directory=None):
    """Render the git ref as `branch:hash` (e.g. `main:abc123`) so the commit
    hash is visually distinct from the session-id badge on line 1. The hash is
    tinted a muted tan while the branch keeps the default colour. On a detached
    HEAD there is no branch, so just the short hash is shown."""
    if not cwd:
        return ""
    branch, short_hash = _git_ref_raw_cached(cwd, state_directory)
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


def _line1(
    d,
    cwd,
    cwd_display,
    spinner,
    terminal_width_hint=None,
    turns_summary="",
    state_directory=None,
    session_count=None,
):
    host = f"{_HOST_COLOR}{hostname()}{RESET}"
    line1 = (
        f"{spinner} {ORANGE}LOCAL{RESET} [{host}] {cwd_display}"
        if is_local_mode()
        else f"{spinner} [{host}] {cwd_display}"
    )
    # Suppress the brief 2-process overlap during a session restart (old process
    # still winding down as the new one starts) -- only badge a sustained count.
    active_session_count = (
        count_active_sessions(cwd) if session_count is None else session_count
    )
    n_sessions = debounce_session_count(active_session_count, cwd)
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    ref = _git_ref(cwd, state_directory)
    if ref:
        line1 = f"{line1} ({ref})"
    # Antigravity CLI only: a muted [glyph state] tag from its `agent_state`
    # field. "" (thus a no-op) on every other harness, which carries no such
    # field.
    state_tag = format_agent_state(d.get("agent_state"))
    if state_tag:
        line1 = f"{line1} {state_tag}"
    session_id = d.get("session_id") or d.get("conversation_id")
    session_name = d.get("session_name") or d.get("session_title") or d.get("title")
    line1 = _append_session_id(line1, session_id)
    line1 = _append_turn_count(line1, turns_summary)
    return _append_session_name(line1, session_name, terminal_width_hint)


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
    sid = str(session_id or "").strip()
    if not sid:
        return line1
    return f"{line1} {_SESSION_ID_COLOR}[{sid[:_SESSION_ID_LEN]}]{RESET}"


def _append_turn_count(line1, turns_summary):
    """Append the session turn counter (`N turns`, or `N steps` when the
    transcript carried no user entries) after the session-id badge. Like the
    id badge it is tiny and unconditional: not width-gated, unlike the title.
    "" when the walk produced no turn data (missing transcript) - a no-op."""
    if not turns_summary:
        return line1
    return f"{line1} {turns_summary}"


def _append_session_name(line1, session_name, terminal_width_hint=None):
    """Append the auto-generated session title after the path/branch, but only
    when it fits. Width comes from `$COLUMNS` (the same source line 2 uses),
    falling back to `terminal_width_hint` (Antigravity CLI's payload-carried
    width) when unset; if neither is available we append best-effort. The
    title is the first thing to yield - it is a nicety, never worth pushing
    the path off screen - so on a known-too-narrow terminal it is dropped
    entirely. Long titles are clipped to keep line 1 bounded even when width
    is unknown."""
    name = str(session_name or "").strip()
    if not name:
        return line1
    if len(name) > _SESSION_NAME_MAX:
        name = name[: _SESSION_NAME_MAX - 1] + "…"
    segment = f" {_SESSION_NAME_COLOR}{name}{RESET}"
    cols = terminal_columns(terminal_width_hint)
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


def context_usage(payload):
    """(context_used, window_size, current_usage) from context_window.

    Anchored on token counts rather than the payload's used_percentage, which
    rounds to whole percent (10K tokens of slop on a 1M window)."""
    context_window = payload.get("context_window") or {}
    window_size = context_window.get("context_window_size") or 200_000
    current_usage = context_window.get("current_usage") or {}
    context_used = (
        (current_usage.get("input_tokens") or 0)
        + (current_usage.get("cache_creation_input_tokens") or 0)
        + (current_usage.get("cache_read_input_tokens") or 0)
    )
    return context_used, window_size, current_usage


def transcript_path_for(payload):
    """The session transcript path: the payload's own field, else the
    filename scan by session id. "" when neither resolves."""
    path = payload.get("transcript_path")
    if path:
        return path
    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    if not session_id:
        return ""
    return _find_session_jsonl(session_id) or ""


def render_claude_statusline(
    payload, cwd, walk, now, *, state_directory=None, session_count=None
):
    """Render the Claude Code and Antigravity CLI status line from an already
    computed transcript `walk`.

    The walk is a parameter, not something this function computes: the
    resident server maintains one accumulator per session and folds in only
    the transcript bytes appended since the last render, so re-walking here
    would undo the entire point of the server."""
    workspace = payload.get("workspace") or {}
    # current_dir follows shell `cd`; project_dir is the fixed launch dir.
    cwd_display = _format_cwd(workspace.get("project_dir") or "", cwd)

    # --- Context: anchored on token counts (avoids the 1% rounding in the
    # payload's used_percentage -- 10K-token slop on a 1M window).
    context_used, window_size, current_usage = context_usage(payload)
    model_obj = payload.get("model") or {}
    model_id = model_obj.get("id") or ""
    model_summary = format_model_badge(model_id, model_obj.get("display_name") or "")

    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    transcript_path = transcript_path_for(payload)

    # Payload total_cost_usd is parent-only (Claude Code issue #48040: subagents
    # are isolated sessions). Pair it with our subagent estimate; walk["parent_cost"]
    # lets us flag drift.
    cost = payload.get("cost") or {}
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
    rate_limits = payload.get("rate_limits")
    day_budget_summary = format_day_budget(rate_limits)

    # Antigravity CLI carries terminal width in the payload (no $COLUMNS env
    # var); Claude Code sets $COLUMNS directly, which always wins when present
    # (see compact.py). Threaded through both line 1's title fit-check and
    # line 2's compact-mode resolution.
    terminal_width_hint = payload.get("terminal_width")
    is_agy = payload.get("product") == "antigravity"

    spinner = spinner_frame()
    turns_summary = format_turn_count(walk["user_prompts"], walk["assistant_turns"])
    line1 = _line1(
        payload,
        cwd,
        cwd_display,
        spinner,
        terminal_width_hint,
        turns_summary,
        state_directory,
        session_count=session_count,
    )

    # Resolve compact verbosity (STATUSLINE_COMPACT + $COLUMNS): re-render the
    # already-walked data at each flag set until it fits, then render once more.
    line2_inputs = Line2Inputs(
        model_summary,
        context_used,
        window_size,
        model_id,
        walk,
        rate_limits,
        day_budget_summary,
        cost_summary,
        hide_cost(),
        lines_summary,
        agy_quota=payload.get("quota"),
        current_usage=current_usage,
        is_agy=is_agy,
    )
    flags = resolve_flags(lambda f: render_line2(f, line2_inputs), terminal_width_hint)
    line2 = render_line2(flags, line2_inputs)

    # Line 3: session wall/api timing (always available), then the weekly-quota
    # exhaustion clock (only past 90% and projected to run out before reset),
    # then the live turn beacon + calibrated ETA (only while a turn is in
    # flight). Any may be absent; join with the same separator the beacon uses.
    line3 = "  ·  ".join(
        part
        for part in (
            format_session_timing(cost),
            weekly_exhaustion(rate_limits),
            format_teammates(session_id, transcript_path, app_dir(), now),
            _beacon_line(session_id),
            format_render_suffix(session_id, state_directory),
        )
        if part
    )

    return "\n".join(part for part in (line1, line2, line3) if part)
