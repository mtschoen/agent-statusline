"""subagentStatusLine entry point. Reads Claude Code's per-tick payload from
stdin (base hook fields + `columns` + `tasks[]`) and emits one NDJSON line
per visible row, replacing the default `name . description . token count`
body with `<icon> <model-badge> <description> | ctx | cache | cost | elapsed`.

Per-task cost is derived from the agent JSONL since the per-task fields don't
include cost. Cache totals come from the same walk; ctx comes from the most
recent assistant turn's usage (matches how the main script reads
`current_usage` from the live payload). description / status / startTime come
straight from the per-tick payload.

Lead-row handling (agent teams):
  Empirically the captured payloads have only teammate tasks (type='local_agent').
  Agent-teams docs describe the lead appearing in the panel ("Shift+Down wraps
  back to the lead") but don't say whether the lead is in `tasks[]`. If it is,
  detect via `task.id == payload.session_id` (the lead's session id IS the
  parent session) or `task.type in {lead, main, coordinator}` and walk
  `transcript_path` directly. If the lead is never in `tasks[]`, the
  detection is dormant -- nothing rendered, nothing broken.

Agent JSONL discovery (teammates):
  task.id maps directly to the on-disk filename: `agent-<task.id>.jsonl`
  under `<parent_transcript_path - ".jsonl">/subagents/`. Verified against
  existing files where the meta + JSONL header carries `agentId == filename id`.
"""

import json
import os
import sys
import time

from statusline_lib import (
    CTX_DENOM,
    GREEN,
    ORANGE,
    RED,
    RESET,
    YELLOW,
    ctx_window_for_model,
    format_beacon,
    format_cache,
    format_context,
    format_cost,
    format_model_badge,
    walk_transcript,
)

_INPUT_LOG = os.path.expanduser("~/.claude/.subagent-statusline-input.log")
_ERROR_LOG = os.path.expanduser("~/.claude/.subagent-statusline-error.log")
# Main statusline writes its latest payload here -- carries the authoritative
# context_window_size, which the lead's transcript can't tell us (the JSONL
# stores `claude-opus-4-7`, dropping the `[1m]` runtime tier suffix).
_MAIN_INPUT_LOG = os.path.expanduser("~/.claude/.statusline-input.log")

# Status -> (icon, color). Substring match so `running` / `in_progress` /
# `completed` / `succeeded` / `error` / `failed` / `paused` / `queued` /
# `pending` all hit the right bucket regardless of exact spelling.
_STATUS_ICON = [
    (("complet", "success", "done", "ok"), "✓", GREEN),
    (("error", "fail"), "✗", RED),
    (("paus",), "⏸", YELLOW),
    (("queu", "pend", "wait"), "○", CTX_DENOM),
]
_DEFAULT_ICON = ("●", YELLOW)  # running / unknown / in-flight

# task.type values that mean "this row is the team lead" rather than a
# teammate. Substring-matched -- substring tolerance covers any future
# `team_lead` / `lead_agent` variants.
_LEAD_TYPES = ("lead", "main", "coordinator")

# Statuses that mean "elapsed clock has stopped" -- omit the elapsed segment
# rather than letting it tick forever past completion (the input gives us no
# `endTime`, so a completed row with `now - startTime` would lie).
_TERMINAL = ("complet", "success", "done", "ok", "error", "fail")


def _safe_write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _agent_jsonl_path(parent_transcript_path, task_id):
    """Return the agent JSONL path or '' if not found.

    Tries the direct mapping first (agentId == task.id, observed empirically),
    falls back to scanning the subagents/ dir if that file doesn't exist --
    cheap insurance against id-format drift.
    """
    if not parent_transcript_path:
        return ""
    base, ext = os.path.splitext(parent_transcript_path)
    if ext.lower() != ".jsonl":
        return ""
    sub_dir = base + "/subagents"
    if not os.path.isdir(sub_dir):
        return ""
    direct = os.path.join(sub_dir, f"agent-{task_id}.jsonl")
    if os.path.exists(direct):
        return direct
    for name in os.listdir(sub_dir):
        if name.startswith("agent-") and name.endswith(".jsonl") and task_id in name:
            return os.path.join(sub_dir, name)
    return ""


def _status_icon(status):
    s = (status or "").lower()
    for keys, icon, color in _STATUS_ICON:
        if any(k in s for k in keys):
            return f"{color}{icon}{RESET}"
    icon, color = _DEFAULT_ICON
    return f"{color}{icon}{RESET}"


def _is_terminal(status):
    s = (status or "").lower()
    return any(k in s for k in _TERMINAL)


def _format_elapsed(start_time_ms, status):
    if _is_terminal(status):
        return ""
    if not start_time_ms or start_time_ms <= 0:
        return ""
    seconds = max(0, int(time.time() * 1000 - start_time_ms) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _live_payload_for_session(session_id):
    """Authoritative window + model id for `session_id`, or None if the
    saved main-statusline payload belongs to another session.

    The transcript JSONL stores `claude-opus-4-7` without the `[1m]` runtime
    tier suffix, so the lead's window can't be inferred from there. The live
    payload carries both fields; one read serves both."""
    if not session_id:
        return None
    try:
        with open(_MAIN_INPUT_LOG, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if d.get("session_id") != session_id:
        return None
    return {
        "window_size": int(
            (d.get("context_window") or {}).get("context_window_size") or 0
        ),
        "model_id": (d.get("model") or {}).get("id") or "",
    }


def _is_lead_task(task, session_id):
    """Detect whether a task entry represents the team lead (the parent
    session itself) rather than a teammate/subagent."""
    task_type = (task.get("type") or "").lower()
    if any(k in task_type for k in _LEAD_TYPES):
        return True
    return bool(session_id and task.get("id") == session_id)


def _row_for_task(task, parent_transcript_path, session_id):
    task_id = task.get("id") or ""
    if not task_id:
        return None

    icon = _status_icon(task.get("status"))
    description = (
        task.get("description")
        or task.get("label")
        or task.get("name")
        or ("(lead)" if _is_lead_task(task, session_id) else "")
    )
    elapsed = _format_elapsed(task.get("startTime"), task.get("status"))

    # Pick the transcript to walk: the parent transcript for the lead row,
    # the per-agent JSONL for everyone else.
    is_lead = _is_lead_task(task, session_id)
    if is_lead:
        jsonl = (
            parent_transcript_path
            if parent_transcript_path and os.path.exists(parent_transcript_path)
            else ""
        )
    else:
        jsonl = _agent_jsonl_path(parent_transcript_path, task_id)

    metric_parts = []
    model_id = ""
    window = 0
    live = _live_payload_for_session(session_id) if is_lead else None
    if jsonl:
        walk = walk_transcript(jsonl, include_subagents=False)
        ctx_used = (
            walk["last_input"] + walk["last_cache_create"] + walk["last_cache_read"]
        )
        # Prefer the authoritative window+model from the live main-statusline
        # payload (lead path only) -- the JSONL drops the [1m] runtime tier.
        model_id = (live or {}).get("model_id") or walk["last_model_id"]
        window = (live or {}).get("window_size") or ctx_window_for_model(
            walk["last_model_id"]
        )
        metric_parts = [
            format_context(ctx_used, window, model_id),
            format_cache(walk["read"], walk["write"], walk["input"]),
            format_cost(walk["cost"]),
        ]

    badge = format_model_badge(model_id)
    head_pieces = [p for p in (icon, badge, description) if p]
    head = " ".join(head_pieces)
    # Beacon: walker globs `agent-<sid>.jsonl`, so the bare task_id is what
    # the lookup expects (no `agent-` prefix on the caller's side).
    beacon_str, _ = format_beacon(task_id)
    parts = [head] + [p for p in metric_parts if p]
    if beacon_str:
        parts.append(beacon_str)
    if elapsed:
        parts.append(elapsed)
    return {"id": task_id, "content": " | ".join(parts)}


def main():
    raw = sys.stdin.read()
    _safe_write(_INPUT_LOG, raw)

    try:
        d = json.loads(raw)
    except Exception:
        d = {}

    parent = d.get("transcript_path") or ""
    session_id = d.get("session_id") or ""
    out = sys.stdout

    _local_mode = os.environ.get("CLAUDE_LOCAL_MODE") == "1" or os.path.isfile(
        os.path.expanduser("~/.claude/.local-mode")
    )
    _local_prefix = f"{ORANGE}LOCAL{RESET} | " if _local_mode else ""

    for task in d.get("tasks") or []:
        row = _row_for_task(task, parent, session_id)
        if row is None:
            continue
        row["content"] = _local_prefix + row["content"]
        out.write(json.dumps(row) + "\n")


def _log_error():
    # Match statusline.py's safety net: a crashing subagent renderer
    # silently drops rows, so log the traceback for later forensics.
    # NDJSON output schema is strict, so we don't emit a visible cue --
    # the parent statusline carries the user-facing error indicator.
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
