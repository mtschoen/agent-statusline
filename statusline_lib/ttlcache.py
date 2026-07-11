"""Generic TTL disk-cache mechanics shared by every single-value, per-key
on-disk cache in this package (currently: statusline.py's git-ref cache and
beacon_cache.py's beacons-latest cache).

Each caller owns its own cache-key -> path mapping and its own payload shape;
this module only owns the two things that were duplicated verbatim between
them: the read-with-TTL-check and the atomic (tmp + os.replace) write, both
of which must never raise -- a cache miss, a corrupt file, or an unwritable
directory all degrade to "recompute this render", never to a crash.

Not a fit for every disk cache in the package: beacon.py's bias-factor cache
and pace.py/burnrate.py's hourly/window-spend caches hold MULTIPLE keyed
entries in one file (so a single TTL check per file doesn't apply), and the
bias cache additionally varies its own TTL per entry (failure vs success).
Forcing those into this single-value contract would need a validation-
predicate parameter no other caller wants -- left as their own copies.
"""

import json
import os
import time

_CACHE_TIMESTAMP_KEY = "cached_at_unix"


def read_ttl_cache(path, ttl_seconds):
    """Return the cached payload dict at `path` (including its internal
    timestamp key) if it exists, parses as a dict, and is within
    `ttl_seconds`; otherwise None. A missing file, corrupt/partial JSON, or
    a non-dict payload are all treated as "no cache" -- never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if time.time() - cached.get(_CACHE_TIMESTAMP_KEY, 0) >= ttl_seconds:
        return None
    return cached


def write_ttl_cache(path, payload):
    """Atomically write `payload` (stamped with the current time) to `path`
    via a pid-scoped tmp file + os.replace, so concurrent writers to the same
    path never see a partial file. Best-effort: a write failure (full disk,
    unwritable dir) is swallowed -- it costs the next read a recompute, never
    breaks the render that just computed `payload`."""
    stamped = {**payload, _CACHE_TIMESTAMP_KEY: time.time()}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stamped, f)
        os.replace(tmp, path)
    except OSError:
        # Best-effort cache write; a failed write must not break rendering,
        # just cost the next read a recompute.
        pass
