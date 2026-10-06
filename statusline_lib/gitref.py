"""Stale-while-revalidate disk-cache around the git subprocess calls the
render paths need: branch name + short hash (`_git_ref_raw_cached`, Claude
line 1) and the working-tree badge counters (`git_working_tree_cached`, Kimi
line 1 -- added/deleted from `git diff --numstat HEAD` plus ahead/behind
against the upstream, mirroring kimi-code's built-in footer badge, which
computes the same numbers in-process and never puts them in the status_line
payload).

The two halves have separate caches and separate refresh kinds because their
costs differ by orders of magnitude: branch/short_hash is two ref lookups,
while `git diff --numstat HEAD` stats the whole working tree (about a second
of every core on a Unity-sized monorepo). The ref half refreshes on a short
TTL. The counter half probes cheaply on the same cadence -- HEAD's hash plus
the index file's (mtime_ns, size), which every commit, add, reset, checkout
and rebase rewrites -- and reruns the diff only when that fingerprint moves
or _GIT_WORKTREE_MAX_AGE_SECONDS has passed. Unstaged edits touch nothing git
tracks, so the max age is what bounds how long the badge can lag one.

Render-perf ratchet step 1 (PLAN.md) TTL-cached this, but a cache miss still
paid the ~9ms git cost inline. Render-perf ratchet step 3 moves the miss/
stale path onto the same in-process refresh pattern as the pace/spend walks
(statusline_lib/server_jobs.py): the render always serves whatever the cache
holds -- an empty ref beats a blocked render -- and a stale/missing entry
hands recomputation to the resident server's worker pool via request_refresh.

Imports:
  base         -- for state_dir
  process_safe -- for run_captured, ProcessTimeout
  server_jobs  -- for request_refresh (in-process cache recompute)
  ttlcache     -- for read_raw_cache / write_ttl_cache mechanics
"""

import hashlib
import os
import time

from .base import state_dir as _resolve_state_dir
from .process_safe import ProcessTimeout, run_captured
from .server_jobs import request_refresh
from .ttlcache import read_raw_cache, write_ttl_cache

_GIT_REF_CACHE_TTL_SECONDS = 2.5
_GIT_WORKTREE_MAX_AGE_SECONDS = 60
_GIT_WORKTREE_COMPUTED_KEY = "computed_at_unix"


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


def _cwd_cache_path(prefix, cwd, state_dir):
    normalized = os.path.normcase(os.path.normpath(cwd))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_resolve_state_dir(state_dir), f"{prefix}-{digest}.json")


def _git_ref_cache_path(cwd, state_dir=None):
    return _cwd_cache_path("gitref", cwd, state_dir)


def _git_worktree_cache_path(cwd, state_dir=None):
    return _cwd_cache_path("gitwt", cwd, state_dir)


def _git_ref_raw_cached(cwd, state_dir=None):
    """Return (branch, short_hash) for cwd -- the cache's raw value, stale
    included, never a synchronous git call. A fresh entry is served as-is; a
    stale or missing entry is served too (stale beats blank beats blocked)
    and hands recomputation to the server's worker pool via request_refresh."""
    path = _git_ref_cache_path(cwd, state_dir)
    cached = read_raw_cache(path)
    if cached is not None:
        branch, short_hash = cached.get("branch", ""), cached.get("short_hash", "")
        age = _cache_age(cached)
        if age < _GIT_REF_CACHE_TTL_SECONDS:
            return branch, short_hash
        request_refresh("git-ref", cwd)
        return branch, short_hash
    request_refresh("git-ref", cwd)
    return "", ""


# Cache keys for the working-tree badge counters, persisted by
# refresh_git_worktree_cache.
_GIT_STAT_KEYS = ("added", "deleted", "ahead", "behind")


def _count(value):
    """Coerce a cached counter to a non-negative int. A hand-edited or
    torn cache (string, bool, negative, null) degrades to 0 -- the render
    must never crash on cache contents it didn't write."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def git_working_tree_cached(cwd, state_dir=None):
    """Return (added, deleted, ahead, behind) for cwd from the git-worktree
    cache -- same stale-while-revalidate contract as _git_ref_raw_cached:
    never a synchronous git call, stale beats blank beats blocked. A stale
    entry requests a refresh, which is a cheap fingerprint probe unless the
    repository actually changed (refresh_git_worktree_cache)."""
    if not cwd:
        return 0, 0, 0, 0
    cached = read_raw_cache(_git_worktree_cache_path(cwd, state_dir))
    if cached is None:
        request_refresh("git-worktree", cwd)
        return 0, 0, 0, 0
    if _cache_age(cached) >= _GIT_REF_CACHE_TTL_SECONDS:
        request_refresh("git-worktree", cwd)
    return tuple(_count(cached.get(key)) for key in _GIT_STAT_KEYS)


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
    """Recompute cwd's branch and short hash and persist them for the
    render's cached read. Runs on the resident server's worker pool
    (server_jobs.run_refresh), never on the render path."""
    path = _git_ref_cache_path(cwd)
    branch = _git_command(cwd, "symbolic-ref", "--short", "HEAD")
    short_hash = _git_command(cwd, "rev-parse", "--short", "HEAD")
    write_ttl_cache(path, {"branch": branch, "short_hash": short_hash})
    return branch, short_hash


def _worktree_fingerprint(cwd):
    """[git_dir, head_hash, index_mtime_ns, index_size] -- one rev-parse and
    one stat, cheap however large the working tree. A failed rev-parse (not
    a repository, unborn HEAD) or a missing index leaves blanks, which still
    compare equal across probes, so the max age alone drives the recompute."""
    lines = _git_command(cwd, "rev-parse", "--absolute-git-dir", "HEAD").splitlines()
    git_dir, head = [*lines, "", ""][:2]
    if not git_dir:
        return ["", "", 0, 0]
    try:
        stat = os.stat(os.path.join(git_dir, "index"))
    except OSError:
        return [git_dir, head, 0, 0]
    return [git_dir, head, stat.st_mtime_ns, stat.st_size]


def _worktree_cache_current(cached, fingerprint):
    return (
        cached is not None
        and cached.get("fingerprint") == fingerprint
        and time.time() - cached.get(_GIT_WORKTREE_COMPUTED_KEY, 0)
        < _GIT_WORKTREE_MAX_AGE_SECONDS
    )


def refresh_git_worktree_cache(cwd):
    """Refresh cwd's working-tree badge counters. Runs on the resident
    server's worker pool, never on the render path. When the fingerprint is
    unchanged and the counters are younger than the max age, the entry is
    rewritten as-is so its probe timestamp moves and the reader stops
    requesting; otherwise the numstat diff and the ahead/behind rev-list
    run. Both fail harmlessly ("" -> zeros) on an unborn HEAD or a branch
    with no upstream. Returns True when the diff ran."""
    path = _git_worktree_cache_path(cwd)
    fingerprint = _worktree_fingerprint(cwd)
    cached = read_raw_cache(path)
    if _worktree_cache_current(cached, fingerprint):
        write_ttl_cache(path, cached)
        return False
    added, deleted = _parse_numstat(
        _git_command(cwd, "diff", "--no-color", "--numstat", "HEAD", "--")
    )
    ahead, behind = _parse_ahead_behind(
        _git_command(cwd, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    )
    write_ttl_cache(
        path,
        {
            "fingerprint": fingerprint,
            _GIT_WORKTREE_COMPUTED_KEY: time.time(),
            "added": added,
            "deleted": deleted,
            "ahead": ahead,
            "behind": behind,
        },
    )
    return True
