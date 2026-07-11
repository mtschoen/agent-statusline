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
  base   -- for app_dir
  walker -- for _walker_subcommand
"""

import json
import os
from datetime import UTC, datetime

from .base import app_dir
from .walker import _walker_subcommand

_BEACON_LATEST_CACHE_DIR = os.path.join(app_dir(), "state")
_BEACON_LATEST_CACHE_TTL_SECONDS = 2.5


def _sanitize_session_id(session_id):
    """Keep session ids filename-safe. They are UUID-ish in practice, but a
    path component should never be built from unsanitized input."""
    return "".join(c for c in str(session_id or "") if c.isalnum() or c in "-_")


def _beacon_latest_cache_path(session_id):
    return os.path.join(
        _BEACON_LATEST_CACHE_DIR,
        f"beacons-latest-{_sanitize_session_id(session_id)}.json",
    )


def _beacons_latest_cached(session_id):
    """Return the beacons-latest walker payload for `session_id`, TTL-cached
    on disk keyed by session id so concurrent sessions never clobber each
    other's entry. A cache miss still pays the walker cost inline; a hit
    skips the subprocess entirely."""
    path = _beacon_latest_cache_path(session_id)
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if (
            isinstance(cached, dict)
            and datetime.now(UTC).timestamp() - cached.get("cached_at_unix", 0)
            < _BEACON_LATEST_CACHE_TTL_SECONDS
        ):
            return cached.get("data")
    except (OSError, ValueError):
        # No cache yet, or a corrupt/partial file -- fall through to a fresh
        # walker read rather than guess at a beacon.
        pass

    # --no-config: this session's transcript is on THIS machine by
    # definition; the SMB extra roots cost 170-190ms per render (uncached,
    # every render) vs ~55ms local-only. Render-budget invariant applies.
    data = _walker_subcommand(
        "beacons-latest", "--session-id", session_id, "--no-config"
    )
    payload = {"cached_at_unix": datetime.now(UTC).timestamp(), "data": data}
    try:
        os.makedirs(_BEACON_LATEST_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        # Best-effort cache write; a failed write must not break rendering,
        # just cost the next render a recompute.
        pass
    return data
