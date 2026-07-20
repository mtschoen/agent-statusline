"""Stale-while-revalidate disk-cache around the two git subprocess calls
`_git_ref` needs (branch name + short hash).

Render-perf ratchet step 1 (PLAN.md) TTL-cached this, but a cache miss still
paid the ~9ms git cost inline. Render-perf ratchet step 3 moves the miss/
stale path onto the same detached-refresher pattern as the pace/spend walks
(statusline_lib/refresh.py): the render always serves whatever the cache
holds -- an empty ref beats a blocked render -- and a stale/missing entry
spawns a detached child to recompute, debounced by refresh.py's inflight
marker. A local git call is fast, not walk-priced, but every render still
paying it inline on each TTL expiry was exactly the class of "residual
per-render work" the <10ms warm-core budget can't afford.

Imports:
  base         -- for state_dir
  process_safe -- for run_captured, ProcessTimeout
  refresh      -- for maybe_spawn_refresh (detached cache recompute)
  ttlcache     -- for read_raw_cache / write_ttl_cache mechanics
"""

import hashlib
import os
import time

from .base import state_dir as _resolve_state_dir
from .process_safe import ProcessTimeout, run_captured
from .refresh import maybe_spawn_refresh
from .ttlcache import read_raw_cache, write_ttl_cache

_GIT_REF_CACHE_TTL_SECONDS = 2.5


def _git_command(cwd, *arguments):
    # run_captured (not subprocess.run) so a git credential-helper grandchild
    # that inherits the stdout pipe can't wedge the refresh past the timeout
    # (bpo-31935 / process_safe's abandon-reader pattern).
    try:
        result = run_captured(["git", "-C", cwd, *arguments], timeout=2)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, ProcessTimeout):
        pass
    return ""


def _git_ref_cache_path(cwd, state_dir=None):
    normalized = os.path.normcase(os.path.normpath(cwd))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_resolve_state_dir(state_dir), f"gitref-{digest}.json")


def _git_ref_raw_cached(cwd, state_dir=None):
    """Return (branch, short_hash) for cwd -- the cache's raw value, stale
    included, never a synchronous git call. A fresh entry is served as-is; a
    stale or missing entry is served too (stale beats blank beats blocked)
    and hands recomputation to a detached child via maybe_spawn_refresh."""
    path = _git_ref_cache_path(cwd, state_dir)
    cached = read_raw_cache(path)
    if cached is not None:
        branch, short_hash = cached.get("branch", ""), cached.get("short_hash", "")
        age = _cache_age(cached)
        if age < _GIT_REF_CACHE_TTL_SECONDS:
            return branch, short_hash
        maybe_spawn_refresh("git-ref", cwd)
        return branch, short_hash
    maybe_spawn_refresh("git-ref", cwd)
    return "", ""


def _cache_age(cached):
    return time.time() - cached.get("cached_at_unix", 0)


def refresh_git_ref_cache(cwd):
    """Recompute cwd's git ref and persist it for the render's cached read.
    Runs in the detached refresh child (refresh.run_refresh), never on the
    render path."""
    state_dir = _resolve_state_dir(None)
    path = _git_ref_cache_path(cwd, state_dir)
    branch = _git_command(cwd, "symbolic-ref", "--short", "HEAD")
    short_hash = _git_command(cwd, "rev-parse", "--short", "HEAD")
    write_ttl_cache(path, {"branch": branch, "short_hash": short_hash})
    return branch, short_hash
