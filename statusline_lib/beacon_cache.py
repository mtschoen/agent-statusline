"""Stale-while-revalidate disk-cache wrapping the beacons-latest walker
lookup.

Split out of beacon.py (which owns the beacon-anchor transcript scan and the
beacons-history bias cache) purely to keep that module under the complexity
gate's line-count threshold -- this file's only concern is the on-disk cache
in front of `_walker_subcommand("beacons-latest", ...)`.

Render-perf ratchet step 2 (PLAN.md) TTL-cached the parsed payload, but a
cache miss still paid the walker subprocess inline (~15-60ms depending on
session size). Render-perf ratchet step 3 moves the miss/stale path onto the
in-process refresh pattern (statusline_lib/server_jobs.py), same as the pace/
spend transcript walks: the render always serves whatever the cache holds --
a hidden beacon column beats a blocked render -- and a stale/missing entry
hands recomputation to the resident server's worker pool via request_refresh.

Imports:
  base        -- for state_dir, sanitize_state_key
  server_jobs -- for request_refresh (in-process cache recompute)
  ttlcache    -- for read_raw_cache / write_ttl_cache mechanics
  walker      -- for _walker_subcommand
"""

import os
import time

from .base import sanitize_state_key
from .base import state_dir as _resolve_state_dir
from .server_jobs import request_refresh
from .ttlcache import read_raw_cache, write_ttl_cache
from .walker import _walker_subcommand

# Independent knob from gitref.py's _GIT_REF_CACHE_TTL_SECONDS -- the two
# happen to share the same 2.5s value today, but they cache unrelated things
# (beacon payloads vs. git refs) and may reasonably diverge later.
_BEACON_LATEST_CACHE_TTL_SECONDS = 2.5


def _beacon_latest_cache_path(session_id, state_dir=None):
    return os.path.join(
        _resolve_state_dir(state_dir),
        f"beacons-latest-{sanitize_state_key(session_id)}.json",
    )


def _beacons_latest_cached(session_id, state_dir=None):
    """Return the beacons-latest walker payload for `session_id` -- the
    cache's raw value, stale included, never a synchronous walker call. A
    fresh entry is served as-is; a stale or missing entry is served too
    (None on a true miss, which format_beacon already treats as "hide the
    column") and hands recomputation to the server's worker pool via
    request_refresh."""
    path = _beacon_latest_cache_path(session_id, state_dir)
    cached = read_raw_cache(path)
    if cached is not None:
        if (
            time.time() - cached.get("cached_at_unix", 0)
            < _BEACON_LATEST_CACHE_TTL_SECONDS
        ):
            return cached.get("data")
        request_refresh("beacon-latest", session_id)
        return cached.get("data")
    request_refresh("beacon-latest", session_id)
    return None


def refresh_beacon_latest_cache(session_id):
    """Recompute `session_id`'s beacons-latest payload and persist it for the
    render's cached read. Runs on the server's worker pool
    (server_jobs.run_refresh), never on the render path.

    --no-config: this session's transcript is on THIS machine by definition;
    the SMB extra roots cost 170-190ms per render vs ~55ms local-only.
    """
    state_dir = _resolve_state_dir(None)
    path = _beacon_latest_cache_path(session_id, state_dir)
    data = _walker_subcommand(
        "beacons-latest", "--session-id", session_id, "--no-config"
    )
    write_ttl_cache(path, {"data": data})
    return data
