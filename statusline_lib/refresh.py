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
import json
import os
import sys
import time

from .base import app_dir
from .process_safe import spawn_detached

# A refresher that died without clearing its marker stops suppressing
# respawns after this long; a healthy one clears the marker on completion.
_INFLIGHT_TTL_SECONDS = 120
_INFLIGHT_PATH = os.path.join(app_dir(), ".statusline-refresh-inflight.json")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    return f"{kind}:{int(argument)}"


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
    (the child inherits no cwd guarantee) and execute the named refresh."""
    return (
        f"import sys; sys.path.insert(0, {_REPO_ROOT!r}); "
        f"from statusline_lib.refresh import run_refresh; "
        f"run_refresh({kind!r}, {float(argument)!r})"
    )


def maybe_spawn_refresh(kind, argument):
    """Start a detached recompute of cache `kind` for `argument` unless one
    is already in flight. Returns True when a child was spawned. A spawn
    failure never surfaces into the render - the claim is released so the
    next render retries."""
    if not _claim_inflight(kind, argument):
        return False
    try:
        spawn_detached([sys.executable, "-c", _child_snippet(kind, argument)])
    except OSError:
        _clear_inflight(kind, argument)
        return False
    return True


def run_refresh(kind, argument):
    """Detached-child entry point: recompute cache `kind`, then clear the
    inflight marker so the next stale render may spawn again. Refreshers are
    imported lazily - pace and burnrate import this module for
    maybe_spawn_refresh, so top-level imports here would be circular."""
    if kind == "pace-hourly":
        from .pace import refresh_pace_hourly_cache as refresher
    elif kind == "window-spend":
        from .burnrate import refresh_window_spend_cache as refresher
    else:
        raise ValueError(f"unknown refresh kind: {kind!r}")
    try:
        refresher(argument)
    finally:
        _clear_inflight(kind, argument)
