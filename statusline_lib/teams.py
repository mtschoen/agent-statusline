"""Agent Teams summary line for the main statusline.

Claude Code's `subagentStatusLine` hook only covers classic Task-tool
subagents (rows with `type: local_agent` in the `tasks[]` payload) --
teammates spawned under `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` never appear in
that payload at all (confirmed empirically 2026-07-02: no
`subagentStatusLine` invocation fired for a live, addressable teammate despite
Claude Code writing its state to `~/.claude/teams/<name>/config.json` in real
time -- see AGENTS.md). There is no per-row hook for teammates to intercept,
so this renders a compact summary on the main statusline instead, using only
files Claude Code already writes.

Imports:
  badge -- format_model_badge
  base  -- color constants
  cost  -- walk_transcript (per-teammate context/cost)
"""

import json
import os

from .badge import format_model_badge
from .base import CTX_DENOM, GREEN, RESET, YELLOW
from .cost import walk_transcript

# Matches Claude Code's own idle-row-hide window (agent-teams doc).
IDLE_THRESHOLD_SECONDS = 30


def _team_name_for_session(session_id):
    """Team dirs are named `session-<first 8 hex chars>` (agent-teams doc)."""
    sid = (session_id or "").strip()
    if not sid:
        return ""
    return f"session-{sid[:8]}"


def _load_team_config(claude_dir, team_name):
    if not team_name:
        return None
    path = os.path.join(claude_dir, "teams", team_name, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _teammate_jsonl(subagents_dir, name):
    """Teammate transcripts are not filed under the config's `agentId`
    (`watchme@session-xxxx`) -- observed on disk as
    `agent-a<name>-<hash>.jsonl`. Substring-match on the bare name, the same
    fallback-scan approach `subagent_statusline.py` uses for task ids."""
    if not name or not os.path.isdir(subagents_dir):
        return ""
    needle = name.lower()
    for fname in sorted(os.listdir(subagents_dir)):
        if (
            fname.startswith("agent-")
            and fname.endswith(".jsonl")
            and needle in fname.lower()
        ):
            return os.path.join(subagents_dir, fname)
    return ""


def _is_active(jsonl_path, now):
    if not jsonl_path:
        return False
    try:
        mtime = os.path.getmtime(jsonl_path)
    except OSError:
        return False
    return (now - mtime) <= IDLE_THRESHOLD_SECONDS


def _teammate_summary(member, subagents_dir, now):
    name = member.get("name") or ""
    if not name:
        return ""
    jsonl = _teammate_jsonl(subagents_dir, name)
    icon = f"{GREEN}●{RESET}" if _is_active(jsonl, now) else f"{CTX_DENOM}○{RESET}"
    badge = format_model_badge(member.get("model") or "")
    cost_part = ""
    if jsonl:
        walk = walk_transcript(jsonl, include_subagents=False)
        if walk["cost"]:
            cost_part = f" {YELLOW}${walk['cost']:.2f}{RESET}"
    pieces = [p for p in (icon, name, badge) if p]
    return " ".join(pieces) + cost_part


def format_teammates(session_id, transcript_path, claude_dir, now):
    """`teammates: <icon> name badge $cost, ...` for line 3, or "" with no
    team or no non-lead members.

    `claude_dir` and `now` are injected rather than defaulted to
    `app_dir()`/`time.time()` so tests never touch the real `~/.claude` tree
    or a live clock.
    """
    team_name = _team_name_for_session(session_id)
    config = _load_team_config(claude_dir, team_name)
    if not config:
        return ""
    members = [
        m for m in (config.get("members") or []) if m.get("agentType") != "team-lead"
    ]
    if not members:
        return ""
    subagents_dir = ""
    if transcript_path:
        base, ext = os.path.splitext(transcript_path)
        if ext.lower() == ".jsonl":
            subagents_dir = base + "/subagents"
    parts = [
        s for s in (_teammate_summary(m, subagents_dir, now) for m in members) if s
    ]
    if not parts:
        return ""
    return f"teammates: {', '.join(parts)}"
