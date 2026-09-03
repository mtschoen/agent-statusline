"""The four harness renders the resident server serves, the last-render file
the client falls back to, and the terminal width one request renders under.

Split out of server.py so that module holds only the protocol (dispatch,
status, housekeeping, shutdown) and neither file approaches the
repository's 400-line limit once Task 13's socket loop lands. Every function
here takes what it needs explicitly rather than reaching for a server, so a
render is testable without one.

Each render is pure formatting over its payload plus the caller's in-memory
tables: no subprocess, no HTTP, no transcript re-walk. That is what lets the
server answer inline on its receive loop. Recomputation of anything
expensive is requested through server_jobs.request_refresh, which the server
has pointed at its bounded worker pool.

Imports:
  base          -- app_dir, safe_write, sanitize_state_key, spinner_frame,
                   state_dir
  kimi / qwen / render_claude / render_subagent -- the harness renders
  nudge         -- write_ctx_state, the wrap-nudge occupancy side channel
  render_subagent -- also _MAIN_INPUT_LOG, the input-log path the subagent
                   render already reads to correlate rows with the main
                   session's last payload
  rendertimer   -- record_render, the previous-render/peak duration store
  server_jobs   -- request_refresh, for the session-count scan
  sessions      -- the session-count cache reader, to age-gate that scan
                   (a private name, the same way render_claude.py reaches
                   for gitref._git_ref_raw_cached)
"""

import contextlib
import json
import os

from .base import app_dir, safe_write, sanitize_state_key, spinner_frame, state_dir
from .kimi import render_kimi_statusline
from .nudge import write_ctx_state
from .qwen import render_qwen_statusline
from .render_claude import context_usage, render_claude_statusline, transcript_path_for
from .render_subagent import _MAIN_INPUT_LOG, render_subagent_rows
from .rendertimer import record_render
from .server_jobs import request_refresh
from .sessions import count_active_sessions, load_session_count_entry

# subagent_statusline.py's own input-log path, distinct from _MAIN_INPUT_LOG
# above (the main claude render's payload). Never read by any lib code
# itself -- it exists purely as the debugging aid AGENTS.md's "compact-mode
# width gate" section documents -- so no prior module defined a constant for
# it; this one does, at import time, the same as _MAIN_INPUT_LOG.
_SUBAGENT_INPUT_LOG = os.path.join(app_dir(), ".subagent-statusline-input.log")

# Shares the "last-render-" prefix server_state.HOUSEKEEPING_PREFIXES sweeps,
# so an ended session's fallback file is not kept forever.
_LAST_RENDER_PREFIX = "last-render-"

# How long a cached session count is served before the server is asked to
# rescan. The psutil walk is machine-wide, so this is the floor on how often
# that single walk runs rather than a per-directory cost, and it sits well
# inside the 20 second dwell sessions.debounce_session_count applies, so an
# elevated count is still observed more than once before the badge arms.
SESSION_COUNT_CACHE_TTL_SECONDS = 10.0


def positive_columns(value):
    """One request's `columns` as a positive integer terminal width, or None.

    Absent, null, zero, negative, non-numeric and infinite all collapse to
    None, which every caller reads as "this client has no width to report"
    rather than as an error: a width is an optimization, never a reason to
    lose a render. OverflowError is in the tuple for the infinite case, which
    a datagram reaches with `"columns": 1e400`.
    """
    try:
        columns = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return columns if columns > 0 else None


@contextlib.contextmanager
def columns_environment(columns):
    """Apply one request's terminal width as $COLUMNS around that request's
    render, restoring whatever this process had before.

    compact.py reads os.environ["COLUMNS"] to decide how much of line 2 fits.
    The harness sets that variable on the process it spawns, which is now the
    thin client, so the width arrives in the request and is installed here
    around the render that reads it. An unusable or absent width REMOVES the
    key rather than leaving the previous request's width in place: a width
    belonging to another terminal is a wrong render, where no width is a full
    one.

    Mutating process-wide state per request is safe only because renders run
    inline on the single receive thread, one at a time; the worker pool runs
    refreshers, never renders. A render moved off that thread would have to
    take the width as an argument instead of through the environment.
    """
    previous = os.environ.get("COLUMNS")
    width = positive_columns(columns)
    if width is None:
        os.environ.pop("COLUMNS", None)
    else:
        os.environ["COLUMNS"] = str(width)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous


def session_count_is_stale(cwd, now, cache_entry):
    """True when `cwd` has no cached session count, or its entry has aged past
    SESSION_COUNT_CACHE_TTL_SECONDS.

    The refresh submission is gated on this. The worker pool deduplicates a
    job only while it is queued or running, so an ungated submit would start
    the next machine-wide process walk the instant the previous one finished,
    forever. An empty cwd is never stale: there is no key to cache by, which
    is the same case count_active_sessions answers with 0.
    """
    if not cwd:
        return False
    if not isinstance(cache_entry, dict):
        return True
    return now - cache_entry.get("ts", 0) >= SESSION_COUNT_CACHE_TTL_SECONDS


def last_render_path(session_id, state_directory=None):
    """Where `session_id`'s most recent reply is kept. The client reads this
    file when the server does not answer in time, resolving the same path
    from its own copy of this rule, so the naming lives in one stated place
    rather than inline at both ends."""
    return os.path.join(
        state_dir(state_directory),
        f"{_LAST_RENDER_PREFIX}{sanitize_state_key(session_id)}.txt",
    )


def write_last_render(session_id, text, state_directory=None):
    """Record `text` as the newest render for `session_id`.

    Written through a temporary file and os.replace because the client reads
    it concurrently and unluckily: half a statusline is worse than a slightly
    old one. Best effort, and skipped for a payload with no session id (Qwen
    carries none), since this file is a client convenience that must never
    cost the reply.
    """
    if not session_id:
        return
    path = last_render_path(session_id, state_directory)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temporary_path, path)
    except OSError:
        # A full disk or unwritable state directory costs the client its
        # fallback file, not this render.
        pass


def write_input_log(payload):
    """Truncate-on-write dump of the latest claude payload to
    `.statusline-input.log` (see render_subagent's _MAIN_INPUT_LOG, which
    reads this same file to correlate subagent rows with the main session's
    last payload, and AGENTS.md's debugging section). The server receives an
    already-parsed payload rather than raw
    stdin text, so this reserializes it with json.dumps. Best effort: a
    failed write must cost only this debugging aid, never the reply, so the
    call is wrapped here rather than trusting safe_write's own internal
    guard, which a test double replacing safe_write itself would bypass.
    """
    with contextlib.suppress(Exception):
        safe_write(_MAIN_INPUT_LOG, json.dumps(payload))


def write_subagent_input_log(payload):
    """The subagent-panel counterpart to write_input_log: dumps the latest
    subagent request's payload to `.subagent-statusline-input.log`. Never
    read by any lib code (unlike _MAIN_INPUT_LOG); it exists solely as a
    debugging aid. Same best-effort
    contract as write_input_log, and for the same reason.
    """
    with contextlib.suppress(Exception):
        safe_write(_SUBAGENT_INPUT_LOG, json.dumps(payload))


def write_debug_input_log(kind, payload):
    """Dispatch a request's payload to whichever `.statusline-*-input.log`
    it belongs on, or nowhere for a kind that never had one. Kept as one
    entry point so Server.handle_request stays a single call rather than
    branching on kind itself."""
    if kind == "claude":
        write_input_log(payload)
    elif kind == "subagent":
        write_subagent_input_log(payload)


def record_render_timing(session_id, elapsed_ms, state_directory):
    """Persist one request's handling duration for the next claude, kimi or
    qwen render's `ui <dur>ms peak <dur>ms` suffix (rendertimer.py).
    "Render" means one request's dispatch inside Server.handle_request, not
    a whole process lifetime.
    record_render already no-ops when timing is disabled and already
    swallows OSError/TypeError/ValueError; this wraps it in Exception too so
    a future change to that contract still cannot cost a reply.
    """
    with contextlib.suppress(Exception):
        record_render(elapsed_ms, session_id, state_dir=state_directory)


def session_id_for(payload):
    """The session id under every spelling the supported harnesses use:
    Claude Code's session_id, Antigravity's conversation_id, Kimi's camelCase
    sessionId. "" when the payload carries none, which is Qwen's case."""
    return (
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("sessionId")
        or ""
    )


def cwd_for(payload):
    """The directory the session sits in: workspace.current_dir, which follows
    shell `cd`, else the flat cwd field the other harnesses send."""
    workspace = payload.get("workspace") or {}
    return workspace.get("current_dir") or payload.get("cwd") or ""


def render_claude_request(payload, tables, clock, state_directory):
    """The Claude Code and Antigravity render: where the state tables meet
    the extracted render.

    The walk is folded from the transcript bytes appended since this
    session's last render rather than re-walked, which is the saving the
    whole server exists for. The session-count refresh is submitted for the
    entire live cwd set at once, because one process walk answers every
    directory and the pool deduplicates by argument. It is submitted only
    when this render's own cwd has gone stale: the pool deduplicates a job
    only while it is queued or running, so an ungated submit would start the
    next machine-wide walk the instant the previous one finished.
    """
    session_id = session_id_for(payload)
    cwd = cwd_for(payload)
    walk = tables.walk_for(
        tables.touch_session(session_id, transcript_path_for(payload))
    )
    tables.touch_cwd(cwd)
    now = clock()
    cache_entry = load_session_count_entry(cwd)
    if session_count_is_stale(cwd, now, cache_entry):
        request_refresh("session-count", tuple(tables.known_cwds()))
    session_count = count_active_sessions(cwd, cache_entry=cache_entry)
    context_used, window_size = context_usage(payload)
    write_ctx_state(
        session_id, context_used, window_size, now, state_dir=state_directory
    )
    text = render_claude_statusline(
        payload,
        cwd,
        walk,
        now,
        state_directory=state_directory,
        session_count=session_count,
    )
    write_last_render(session_id, text, state_directory)
    return text


def render_subagent_request(payload, now):
    """The subagent panel: one JSON row per task, newline joined. No session
    or cwd state is involved, since every row is derived from the payload's
    task list and that task's own agent transcript."""
    return "\n".join(render_subagent_rows(payload, now))


def render_kimi_request(payload, tables, state_directory):
    """Kimi Code CLI's single line. Its TUI renders only the first line of
    stdout, so the adapter produces exactly one."""
    cwd = cwd_for(payload)
    tables.touch_cwd(cwd)
    text = render_kimi_statusline(payload, cwd, spinner_frame())
    write_last_render(session_id_for(payload), text, state_directory)
    return text


def render_qwen_request(payload, tables, state_directory):
    """Qwen Code's two lines, joined the way its entry point printed them.
    Line 2 is empty when every field it holds is absent."""
    cwd = cwd_for(payload)
    tables.touch_cwd(cwd)
    line1, line2 = render_qwen_statusline(payload, cwd, spinner_frame())
    text = "\n".join(part for part in (line1, line2) if part)
    write_last_render(session_id_for(payload), text, state_directory)
    return text
