"""Stale-while-revalidate spawner for the render-path transcript caches.

The render must never walk transcript roots inline: with an SMB extra root
the walk costs seconds, the harness replaces the render subprocess at its
refresh interval (~3s), and a killed render never reaches its cache write -
so the cache can never turn warm and every later render re-pays the walk and
dies the same way. That death spiral froze the statusline at the session's
first pre-token render on 2026-07-16 (`0 / 1.00M`). Cache readers therefore
serve whatever entry they have, stale included, and hand recomputation to a
detached child process that survives the render's kill.

Imports:
  base         -- for app_dir
  process_safe -- for spawn_detached
"""

import contextlib
import importlib
import json
import os
import sys
import time

from .base import app_dir, platform_name
from .process_safe import spawn_detached

# A refresher that died without clearing its marker stops suppressing
# respawns after this long; a healthy one clears the marker on completion.
_INFLIGHT_TTL_SECONDS = 120
_INFLIGHT_PATH = os.path.join(app_dir(), ".statusline-refresh-inflight.json")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Per-render spawn instrumentation: each maybe_spawn_refresh call's
# (kind, elapsed_seconds), consumed by statusline.py's slow-render breakdown
# (PhaseTimer, statusline_lib/rendertimer.py). Module-level rather than
# threaded through every caller's signature -- this function is the one
# choke point every detached refresh spawn funnels through (git-ref,
# beacons-latest, bias-factor, pace-hourly, window-spend, session-count), so
# it is also the natural place to time them. reset_spawn_timings() must be
# called once per render (a fresh spawn-per-render process already starts
# empty; tests and the warm-core benchmark reuse one interpreter across
# several renders and must reset between them).
_SPAWN_TIMINGS = []


def reset_spawn_timings():
    """Clear this render's spawn log."""
    _SPAWN_TIMINGS.clear()


def spawn_timings():
    """This render's (kind, elapsed_seconds) list, oldest first."""
    return list(_SPAWN_TIMINGS)


def _now_unix():
    """Current unix time. Seam so tests can pin the debounce clock."""
    return time.time()


def _read_inflight():
    """The inflight marker dict ({"kind:argument": started_at_unix}); {} when
    absent, unreadable, or not a dict (a torn write must read as "nothing in
    flight", never crash the render)."""
    try:
        with open(_INFLIGHT_PATH, encoding="utf-8") as f:
            marks = json.load(f)
    except (OSError, ValueError):
        return {}
    return marks if isinstance(marks, dict) else {}


def _write_inflight(marks):
    # Best-effort: a lost marker only means one duplicate (idempotent) child.
    with contextlib.suppress(OSError), open(_INFLIGHT_PATH, "w", encoding="utf-8") as f:
        json.dump(marks, f)


def _inflight_key(kind, argument):
    # Numeric arguments (win_start_unix timestamps) are truncated to int so
    # near-identical float representations of "the same window" collapse to
    # one claim; string arguments (a cwd, a session id) are stable as-is.
    if isinstance(argument, (int, float)):
        return f"{kind}:{int(argument)}"
    return f"{kind}:{argument}"


def _claim_inflight(kind, argument):
    """Record (kind, argument) as in flight; False when a live claim already
    exists. Claims older than _INFLIGHT_TTL_SECONDS are pruned on the way."""
    now = _now_unix()
    marks = {
        key: started
        for key, started in _read_inflight().items()
        if now - started < _INFLIGHT_TTL_SECONDS
    }
    key = _inflight_key(kind, argument)
    if key in marks:
        return False
    marks[key] = now
    _write_inflight(marks)
    return True


def _clear_inflight(kind, argument):
    marks = _read_inflight()
    marks.pop(_inflight_key(kind, argument), None)
    _write_inflight(marks)


def _child_snippet(kind, argument):
    """The -c program the detached child runs: import this package by path
    (the child inherits no cwd guarantee) and execute the named refresh.

    Numeric arguments are coerced to float (matches every existing caller,
    window timestamps); string arguments (a cwd, a session id) pass through
    unchanged -- repr() quotes either shape safely for the -c source.

    The render's resolved platform is pinned into the child's env first:
    kimi_statusline.py/qwen_statusline.py inject --statusline-platform into
    sys.argv only, and the child's own argv (python -c) carries no such
    flag -- without this, a kimi/qwen render's refresh child would resolve
    app_dir() to ~/.claude and write the cache where its render never
    reads it (observed 2026-07-28: the kimi working-tree badge stayed cold
    while gitref entries piled up in ~/.claude/state). platform_name()
    returning None (plain Claude render) sets nothing; app_dir()'s default
    is already correct there."""
    arg = float(argument) if isinstance(argument, (int, float)) else argument
    platform = platform_name()
    pin = (
        f"import os; os.environ['STATUSLINE_PLATFORM'] = {platform!r}; "
        if platform
        else ""
    )
    return (
        f"{pin}import sys; sys.path.insert(0, {_REPO_ROOT!r}); "
        f"from statusline_lib.refresh import run_refresh; "
        f"run_refresh({kind!r}, {arg!r})"
    )


def maybe_spawn_refresh(kind, argument):
    """Start a detached recompute of cache `kind` for `argument` unless one
    is already in flight. Returns True when a child was spawned. A spawn
    failure never surfaces into the render - the claim is released so the
    next render retries.

    Every call (successful or not) is timed into _SPAWN_TIMINGS using the
    monotonic clock (immune to wall-clock jumps, unlike _now_unix/_claim_inflight's
    debounce timestamps): even a "fire and forget" Popen still pays real
    CreateProcess cost on the calling process before it returns, and that
    cost is exactly what statusline.py's slow-render breakdown wants
    visibility into.
    """
    if not _claim_inflight(kind, argument):
        return False
    started = time.monotonic()
    try:
        spawn_detached([sys.executable, "-c", _child_snippet(kind, argument)])
    except OSError:
        _clear_inflight(kind, argument)
        return False
    finally:
        _SPAWN_TIMINGS.append((kind, time.monotonic() - started))
    return True


# kind -> (module name within this package, refresher attribute). Modules are
# imported lazily inside run_refresh -- pace/burnrate/gitref/beacon_cache/
# beacon/sessions all import this module for maybe_spawn_refresh, so a
# top-level import here would be circular.
_REFRESHER_MODULES = {
    "pace-hourly": ("pace", "refresh_pace_hourly_cache"),
    "window-spend": ("burnrate", "refresh_window_spend_cache"),
    "git-ref": ("gitref", "refresh_git_ref_cache"),
    "beacon-latest": ("beacon_cache", "refresh_beacon_latest_cache"),
    "session-count": ("sessions", "refresh_session_count_cache"),
    "bias-factor": ("beacon", "refresh_bias_factor_cache"),
}


def run_refresh(kind, argument):
    """Detached-child entry point: recompute cache `kind`, then clear the
    inflight marker so the next stale render may spawn again."""
    target = _REFRESHER_MODULES.get(kind)
    if target is None:
        raise ValueError(f"unknown refresh kind: {kind!r}")
    module_name, attr = target
    module = importlib.import_module(f".{module_name}", package=__package__)
    refresher = getattr(module, attr)
    try:
        refresher(argument)
    finally:
        _clear_inflight(kind, argument)
