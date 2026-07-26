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

Also home to ``PhaseTimer``, a separate and much cheaper concern: a
per-render sequence of named checkpoint durations that ``statusline.py``
feeds into ``_log_slow_render``'s breakdown when a render crosses the
slow-render threshold. Unlike the previous-render/peak state above, it never
touches disk -- it lives and dies within one render's process.
"""

import json
import os
import time

from .base import RESET, sanitize_state_key
from .base import state_dir as _resolve_state_dir
from .refresh import reset_spawn_timings

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


class PhaseTimer:
    """Zero-config phase-checkpoint accumulator for one render's diagnostic
    breakdown (statusline.py's slow-render log line, added after the
    2026-07-26 5.8s spike investigation -- see PLAN.md).

    Two ways to add an entry:
      mark(name, detail=None)      -- times the interval since the previous
                                       mark() (or construction): use for
                                       sequential phases in main()'s body.
      record(name, elapsed_seconds, detail=None)
                                    -- adds an already-known duration for work
                                       that doesn't fit the sequential shape,
                                       e.g. detached-refresh spawns scattered
                                       across the render and summed
                                       independently (statusline_lib.refresh's
                                       spawn_timings()).

    Near-zero cost on the fast path: one monotonic() read and a list append
    per call, no I/O or string formatting until breakdown() is actually
    rendered -- which only happens once a render crosses the slow-render
    threshold.
    """

    def __init__(self):
        self._last = time.monotonic()
        self._entries = []  # [(name, elapsed_seconds, detail|None)]

    def mark(self, name, detail=None):
        now = time.monotonic()
        self._entries.append((name, now - self._last, detail))
        self._last = now

    def record(self, name, elapsed_seconds, detail=None):
        self._entries.append((name, elapsed_seconds, detail))

    def breakdown(self):
        """`name=1.23s[detail]` entries joined with ', ', or '' when no
        entries were ever recorded."""
        parts = []
        for name, elapsed, detail in self._entries:
            label = f"{name}={elapsed:.2f}s"
            if detail:
                label += f"[{detail}]"
            parts.append(label)
        return ", ".join(parts)


_CURRENT_PHASE_TIMER = None


def start_phase_timer():
    """Begin a new render's instrumentation: resets refresh.py's per-render
    spawn log (statusline_lib.refresh.reset_spawn_timings) and starts a
    fresh PhaseTimer, replacing any prior one -- one call at the top of
    statusline.py's main() instead of three. Returns the new PhaseTimer;
    current_phase_timer() is how the __main__ block reaches it later
    (after main() returns or raises), since it doesn't hold the return
    value itself."""
    reset_spawn_timings()
    global _CURRENT_PHASE_TIMER
    _CURRENT_PHASE_TIMER = PhaseTimer()
    return _CURRENT_PHASE_TIMER


def current_phase_timer():
    """The active render's PhaseTimer, or None before start_phase_timer()
    has ever run (e.g. main() raised before reaching it)."""
    return _CURRENT_PHASE_TIMER


def summarize_spawns(phase_timer, spawns, phase_name="beacon", bias_kind="bias-factor"):
    """Fold this render's refresh.spawn_timings() into `phase_timer`: marks
    `phase_name` (the section of main() that can trigger a `bias_kind`
    respawn -- _beacon_line's calibrated ETA, by default) with a
    "bias-refresh-spawned" detail when that specific kind fired during this
    render, and records an aggregate "spawns" entry (count + total elapsed)
    when any spawn happened at all, of any kind.

    Kept here rather than inlined in statusline.py's main() -- glue that
    threads two module-level reads into a PhaseTimer call is worth a name,
    and main() is long enough already (statusline.py is coverage-exempt
    entry glue, so this move also puts real 100%-covered-suite teeth on the
    logic for the first time).
    """
    bias_spawned = any(kind == bias_kind for kind, _elapsed in spawns)
    phase_timer.mark(
        phase_name, detail="bias-refresh-spawned" if bias_spawned else None
    )
    if spawns:
        phase_timer.record(
            "spawns",
            sum(elapsed for _kind, elapsed in spawns),
            detail=f"{len(spawns)}x",
        )
