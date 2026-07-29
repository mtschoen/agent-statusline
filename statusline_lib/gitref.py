"""Stale-while-revalidate disk-cache around the git subprocess calls the
render paths need: branch name + short hash (`_git_ref_raw_cached`, Claude
line 1) and the working-tree badge counters (`git_working_tree_cached`, Kimi
line 1 -- added/deleted from `git diff --numstat HEAD` plus ahead/behind
against the upstream, mirroring kimi-code's built-in footer badge, which
computes the same numbers in-process and never puts them in the status_line
payload).

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


# Cache keys for the working-tree badge counters, persisted by
# refresh_git_ref_cache alongside branch/short_hash.
_GIT_STAT_KEYS = ("added", "deleted", "ahead", "behind")


def _count(value):
    """Coerce a cached counter to a non-negative int. A hand-edited or
    torn cache (string, bool, negative, null) degrades to 0 -- the render
    must never crash on cache contents it didn't write."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def git_working_tree_cached(cwd, state_dir=None):
    """Return (added, deleted, ahead, behind) for cwd from the git-ref
    cache -- same stale-while-revalidate contract as _git_ref_raw_cached:
    never a synchronous git call, stale beats blank beats blocked. An entry
    predating the badge counters (keys absent) is served as zeros and
    backfilled by the detached refresh it triggers."""
    if not cwd:
        return 0, 0, 0, 0
    path = _git_ref_cache_path(cwd, state_dir)
    cached = read_raw_cache(path)
    if cached is None:
        maybe_spawn_refresh("git-ref", cwd)
        return 0, 0, 0, 0
    stats = tuple(_count(cached.get(key)) for key in _GIT_STAT_KEYS)
    if _cache_age(cached) >= _GIT_REF_CACHE_TTL_SECONDS or any(
        key not in cached for key in _GIT_STAT_KEYS
    ):
        maybe_spawn_refresh("git-ref", cwd)
    return stats


def _cache_age(cached):
    return time.time() - cached.get("cached_at_unix", 0)


def _parse_numstat(text):
    """Sum added/deleted counts from `git diff --numstat` output. Each line
    is `<added>\\t<deleted>\\t<path>`; binary files report `-` for both and
    count as 0. Blank/garbage lines are skipped, never fatal."""
    added = deleted = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added += int(parts[0]) if parts[0].isdigit() else 0
        deleted += int(parts[1]) if parts[1].isdigit() else 0
    return added, deleted


def _parse_ahead_behind(text):
    """Parse `git rev-list --left-right --count @{upstream}...HEAD` output
    (`<behind>\\t<ahead>`) into (ahead, behind). Empty output (no upstream,
    unborn HEAD -- _git_command already degraded the non-zero exit to "")
    or a wrong shape degrades to (0, 0)."""
    parts = text.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return 0, 0
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


def refresh_git_ref_cache(cwd):
    """Recompute cwd's git ref and working-tree counters and persist them
    for the render's cached read. Runs in the detached refresh child
    (refresh.run_refresh), never on the render path. The numstat and
    rev-list calls fail harmlessly ("" -> zeros) on an unborn HEAD or a
    branch with no upstream."""
    state_dir = _resolve_state_dir(None)
    path = _git_ref_cache_path(cwd, state_dir)
    branch = _git_command(cwd, "symbolic-ref", "--short", "HEAD")
    short_hash = _git_command(cwd, "rev-parse", "--short", "HEAD")
    added, deleted = _parse_numstat(
        _git_command(cwd, "diff", "--no-color", "--numstat", "HEAD", "--")
    )
    ahead, behind = _parse_ahead_behind(
        _git_command(cwd, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    )
    write_ttl_cache(
        path,
        {
            "branch": branch,
            "short_hash": short_hash,
            "added": added,
            "deleted": deleted,
            "ahead": ahead,
            "behind": behind,
        },
    )
    return branch, short_hash
