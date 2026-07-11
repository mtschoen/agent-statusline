"""TTL disk-cache wrapping the beacons-latest walker lookup.

Split out of beacon.py (which owns the beacon-anchor transcript scan and the
beacons-history bias cache) purely to keep that module under the complexity
gate's line-count threshold -- this file's only concern is the on-disk cache
in front of `_walker_subcommand("beacons-latest", ...)`.

Render-perf ratchet step 2 (PLAN.md): beacons-latest costs ~60ms/render
local, uncached. TTL-caching the parsed payload on disk, keyed by session id,
makes that cost invisible at statusline cadence (~300ms refresh). A cached
`age_seconds` can be up to the TTL stale -- acceptable, since the staleness
threshold that matters (beacon.py's `_BEACON_STALE_SECONDS`, 5 minutes) is
two orders of magnitude looser.

Imports:
  base     -- for state_dir, sanitize_state_key
  ttlcache -- for the shared TTL-check + atomic-write mechanics
  walker   -- for _walker_subcommand
"""

import os

from .base import sanitize_state_key
from .base import state_dir as _resolve_state_dir
from .ttlcache import read_ttl_cache, write_ttl_cache
from .walker import _walker_subcommand

# Independent knob from statusline.py's _GIT_REF_CACHE_TTL_SECONDS -- the two
# happen to share the same 2.5s value today, but they cache unrelated things
# (beacon payloads vs. git refs) and may reasonably diverge later.
_BEACON_LATEST_CACHE_TTL_SECONDS = 2.5


def _beacon_latest_cache_path(session_id, state_dir=None):
    return os.path.join(
        _resolve_state_dir(state_dir),
        f"beacons-latest-{sanitize_state_key(session_id)}.json",
    )


def _beacons_latest_cached(session_id, state_dir=None):
    """Return the beacons-latest walker payload for `session_id`, TTL-cached
    on disk keyed by session id so concurrent sessions never clobber each
    other's entry. A cache miss still pays the walker cost inline; a hit
    skips the subprocess entirely."""
    path = _beacon_latest_cache_path(session_id, state_dir)
    cached = read_ttl_cache(path, _BEACON_LATEST_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached.get("data")

    # --no-config: this session's transcript is on THIS machine by
    # definition; the SMB extra roots cost 170-190ms per render (uncached,
    # every render) vs ~55ms local-only. Render-budget invariant applies.
    data = _walker_subcommand(
        "beacons-latest", "--session-id", session_id, "--no-config"
    )
    # A falsy/None walker result is cached too: a session that starts
    # emitting a beacon mid-window stays hidden for up to the TTL (2.5s),
    # which is invisible at statusline cadence and keeps misses cheap.
    write_ttl_cache(path, {"data": data})
    return data
