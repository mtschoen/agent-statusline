"""Render-duration instrumentation for the spawn-per-render Python harnesses,
mirroring the Pi extension's footer timer (``pi-extension/renderer.ts``,
``installStatuslineFooter``, commit 0323dbc).

Pi is a long-lived process: it measures its own render in flight and appends
that duration to the NEXT render's footer. The Python harnesses (statusline.py,
qwen_statusline.py) spawn fresh per render, so the current render's duration is
only known after stdout is already written. This module keeps the same
previous-render semantics anyway: ``format_render_suffix`` reads the PRIOR
render's duration + this session's peak (call it before printing), and
``record_render`` persists the render just finished for the next process to
pick up (call it at process exit, after printing).

Peak tracking is per-session by construction: each session id gets its own
state file (see ``render_timer_path``), so a new session id starts with no
prior file and therefore no inherited peak -- no separate reset step needed.
A harness with no session id in its payload (Qwen) collapses onto one shared
file.

State lives under ``~/.claude/state`` (override with ``CLAUDE_STATE_DIR``),
the same directory ``nudge.py`` uses for the wrap-nudge state -- the resolver
is shared (``base.state_dir``) rather than re-implemented.
"""

import json
import os

from .base import RESET, sanitize_state_key
from .base import state_dir as _resolve_state_dir

# Same env var name and default-on/"0"-disables semantics as the Pi footer.
RENDER_TIMING_ENV_VAR = "STATUSLINE_RENDER_TIMING"

# 256-color grey -- matches Pi's DIM (`\x1b[38;5;245m`) and statusline.py's
# existing muted-label color, so the suffix reads as secondary text.
_DIM = "\x1b[38;5;245m"

_SHARED_KEY = "shared"


def timing_enabled():
    return os.environ.get(RENDER_TIMING_ENV_VAR) != "0"


def _sanitize(session_id):
    """Thin wrapper around the shared sanitizer: empty/absent ids collapse
    onto one shared key (Qwen's payload carries no session id at all)."""
    return sanitize_state_key(session_id) or _SHARED_KEY


def render_timer_path(session_id=None, state_dir=None):
    return os.path.join(
        _resolve_state_dir(state_dir), f"render-timer-{_sanitize(session_id)}.json"
    )


def read_previous(session_id=None, state_dir=None):
    """Return ``(last_ms, peak_ms)`` from the previous render, or None when
    there is no usable prior state (first render, corrupt file, or a state
    dir that isn't writable/readable -- all treated as "no signal")."""
    path = render_timer_path(session_id, state_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return None
    except ValueError:
        # Corrupt or partial JSON -- ignore rather than guess a duration.
        return None
    last = data.get("last_ms")
    peak = data.get("peak_ms")
    if not isinstance(last, (int, float)) or not isinstance(peak, (int, float)):
        return None
    return (float(last), float(peak))


def record_render(elapsed_ms, session_id=None, state_dir=None):
    """Persist this render's duration + the updated session peak for the next
    process to read. Best-effort and must never raise: a bad ``elapsed_ms``,
    a full disk, or an unwritable state dir should cost us the NEXT render's
    timing suffix, never break the render calling this at process exit."""
    if not timing_enabled():
        return
    try:
        elapsed_ms = float(elapsed_ms)
        previous = read_previous(session_id, state_dir)
        peak_ms = max(elapsed_ms, previous[1] if previous else 0.0)
        path = render_timer_path(session_id, state_dir)
        payload = {"last_ms": elapsed_ms, "peak_ms": peak_ms}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def format_render_suffix(session_id=None, state_dir=None):
    """Return the dim `ui <dur> peak <dur>` suffix for the previous render
    (Pi's exact wording, see renderer.ts line3), or "" when disabled or no
    prior render exists yet."""
    if not timing_enabled():
        return ""
    previous = read_previous(session_id, state_dir)
    if previous is None:
        return ""
    last_ms, peak_ms = previous
    return f"{_DIM}ui {last_ms:.2f}ms peak {peak_ms:.2f}ms{RESET}"
