"""Wrap nudge: the shared state-file contract between the statusline (producer)
and the UserPromptSubmit hook (consumer).

Only the statusline's stdin payload carries live context-window occupancy
(``context_window.current_usage``); a UserPromptSubmit hook's payload does not.
So the statusline writes the occupancy to a per-session state file on every
render, and the nudge hook reads that file -- no transcript walk required.

The threshold is 250K tokens. There is NO pricing cliff here: the Opus
1M-context tier bills at a flat per-token rate (verified 2026-06 against current
Anthropic docs and against ~/.claude.json billing across 28 Opus[1m] sessions --
no 2x surcharge above 200K). So the nudge is about context hygiene, not cost
avoidance: long sessions accumulate cache-read tokens on every turn (real spend,
just not penalized) and model recall degrades as the window fills, so ~250K is a
sensible point to start offering a clean wrap before the ~300K soft ceiling.

State lives under ``~/.claude/state`` (override with ``CLAUDE_STATE_DIR`` for
tests). Both files are keyed by session id so concurrent sessions never clobber
each other's occupancy or one-shot marker.
"""

import json
import os

from .base import app_dir

# Context-hygiene caution line; also the statusline yellow anchor on 1M models.
# NOT a pricing boundary -- the 1M tier bills flat (see module docstring).
NUDGE_THRESHOLD_TOKENS = 250_000

# Soft ceiling the nudge text points at; past here a wrap is overdue.
NUDGE_SOFT_CEILING_TOKENS = 300_000


def format_nudge(ctx_used):
    """UserPromptSubmit additionalContext: offer /wrap, never run it
    (user-initiated). Reports live occupancy so the agent can frame the
    suggestion concretely instead of citing a bare threshold."""
    used_k = round((ctx_used or 0) / 1000)
    ceiling_k = NUDGE_SOFT_CEILING_TOKENS // 1000
    return (
        f"This session is around {used_k}K tokens of context. Long sessions "
        "accumulate cache-read cost on every turn and model recall degrades as "
        f"the window fills, so past ~{ceiling_k}K it is usually worth winding "
        "down. If you are at a natural stopping point, consider suggesting "
        "`/wrap` to the user to close the session cleanly. Do not interrupt "
        "in-progress work -- finish the current thread first, and skip the "
        "suggestion entirely if a wrap was already offered or declined this "
        "session."
    )


def _state_dir(state_dir=None):
    """Resolve the state directory: explicit arg > CLAUDE_STATE_DIR > default."""
    return (
        state_dir
        or os.environ.get("CLAUDE_STATE_DIR")
        or os.environ.get("ANTIGRAVITY_STATE_DIR")
        or os.path.join(app_dir(), "state")
    )


def _sanitize(session_id):
    """Keep session ids filename-safe. They are UUID-ish in practice, but a path
    component should never be built from unsanitized input."""
    return "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")


def ctx_state_path(session_id, state_dir=None):
    return os.path.join(_state_dir(state_dir), f"ctx-{_sanitize(session_id)}.json")


def marker_path(session_id, state_dir=None):
    return os.path.join(_state_dir(state_dir), f"wrap-nudge-{_sanitize(session_id)}")


def write_ctx_state(session_id, ctx_used, window_size, now, state_dir=None):
    """Persist live context occupancy for ``session_id``. Called from the
    statusline on every render, so it is best-effort: a state-dir failure must
    degrade the nudge, never the statusline render."""
    if not session_id:
        return
    path = ctx_state_path(session_id, state_dir)
    payload = {"ctx_used": int(ctx_used), "window_size": int(window_size), "ts": now}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        # Non-essential side channel; a full disk or unwritable state dir should
        # cost us the nudge, not the statusline the user is looking at.
        pass


def read_ctx_used(session_id, state_dir=None):
    """Return the last-written ``ctx_used`` for ``session_id``, or None when the
    file is absent or unreadable (treated as "no signal", so no nudge)."""
    path = ctx_state_path(session_id, state_dir)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        # No file yet (the statusline has not rendered) -- nothing to act on.
        return None
    except ValueError:
        # Corrupt or partial JSON -- ignore rather than guess an occupancy.
        return None
    value = data.get("ctx_used")
    return int(value) if isinstance(value, (int, float)) else None


def should_nudge(
    ctx_used, session_id, state_dir=None, threshold=NUDGE_THRESHOLD_TOKENS
):
    """One-shot gate: True iff occupancy is at/above the threshold and this
    session has not already been nudged (no marker file yet)."""
    if ctx_used is None or ctx_used < threshold:
        return False
    return not os.path.exists(marker_path(session_id, state_dir))


def write_marker(session_id, state_dir=None):
    """Record that this session has been nudged, so it fires at most once."""
    path = marker_path(session_id, state_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("fired\n")
    except OSError:
        # Worst case if the marker can't be written is a repeat nudge on a later
        # prompt -- acceptable, and not worth failing the hook over.
        pass
