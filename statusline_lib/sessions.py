"""Session counting and debounce helpers.

Detects other Claude Code sessions running in the same cwd so the
statusline can warn that a second interactive instance is active here.

Enumerates `claude` processes whose own cwd matches and which are not in
`-p` headless mode (Task subagents, scripted runs). Ground truth -- catches
idle sessions, ignores ones that cleanly /exit'd a moment ago. Requires
`psutil`; without it the badge stays off entirely (any mtime-based
substitute false-positives for ~5 minutes after a clean /exit, which the
20s restart-handoff debounce can't suppress).
"""

import json
import os
import time

_psutil = None  # cached module handle within a process; None if unavailable.


def _resolve_psutil():
    """Import psutil on first use; return the module, or None if unavailable."""
    global _psutil
    if _psutil is None:
        try:
            import psutil as module
        except ImportError:
            return None
        _psutil = module
    return _psutil


_SESSION_COUNT_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-sessioncount-cache.json"
)
_SESSION_COUNT_CACHE_TTL_SECONDS = 8
_SESSION_COUNT_CACHE_MAX_AGE_SECONDS = 86400  # prune entries older than a day


def _load_session_count_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session_count_cache(path, cache, now):
    pruned = {
        k: v
        for k, v in cache.items()
        if isinstance(v, dict)
        and (now - v.get("ts", 0)) <= _SESSION_COUNT_CACHE_MAX_AGE_SECONDS
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except OSError:
        # Best-effort cache write; an unwritable/missing cache dir is non-fatal
        # and must never break statusline rendering.
        pass


def count_active_sessions(
    cwd, *, now=None, cache_path=None, ttl=_SESSION_COUNT_CACHE_TTL_SECONDS
):
    """Return how many interactive Claude sessions are running in `cwd`.

    Memoized on disk for `ttl` seconds keyed by cwd. Returns 0 when psutil is
    unavailable, `cwd` is empty, or any error occurs -- never raises (statusline
    rendering must not crash).
    """
    if not cwd:
        return 0
    now = time.time() if now is None else now
    path = cache_path or _SESSION_COUNT_CACHE_PATH
    key = os.path.normcase(cwd)

    cache = _load_session_count_cache(path)
    entry = cache.get(key)
    # Clock-skew guard: a future-stamped entry (now - ts < 0) is treated as a
    # miss so a backwards clock jump can't pin a stale count indefinitely.
    if isinstance(entry, dict) and 0 <= (now - entry.get("ts", 0)) < ttl:
        return int(entry.get("count", 0))

    psutil = _resolve_psutil()
    if psutil is None:
        return 0
    try:
        count = _count_via_psutil(cwd, psutil)
    except Exception:
        return 0
    cache[key] = {"count": count, "ts": now}
    _save_session_count_cache(path, cache, now)
    return count


def _process_matches(name, cmdline, cwd, target_cwd):
    """Pure classifier: does this (name, cmdline, cwd) tuple represent an
    interactive Claude session rooted at `target_cwd`? Extracted so unit
    tests don't need a live or mocked psutil."""
    n = (name or "").lower()
    if n not in ("claude", "claude.exe", "node", "node.exe"):
        return False
    cl = cmdline or ()
    if n in ("node", "node.exe") and not any(
        "claude" in (arg or "").lower() for arg in cl
    ):
        return False
    if "-p" in cl or "--print" in cl:
        return False
    if not cwd:
        return False
    return os.path.normcase(cwd) == os.path.normcase(target_cwd)


def _count_via_psutil(target_cwd, psutil):
    count = 0
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        # Cheap name pre-filter -- avoids calling cmdline()/cwd() on every
        # process (hundreds on a typical box).
        if name not in ("claude", "claude.exe", "node", "node.exe"):
            continue
        try:
            cmdline = p.cmdline()
            pcwd = p.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if _process_matches(name, cmdline, pcwd, target_cwd):
            count += 1
    return count


# `count_active_sessions` reports live process truth, but a restart produces a
# brief handoff overlap: the old `claude` process is still winding down when
# the new one spins up, so for a few seconds two processes legitimately match
# the cwd. Painting `[2 sessions]` for that blip is noise. We suppress the
# badge until an elevated (>= 2) count has *persisted* for the dwell window.
#
# The statusline re-renders only when a turn is processed, so the dwell is
# timed against a stored wall-clock timestamp, never a render count. State is
# a small JSON file keyed by cwd: {cwd: {"first": ts, "last": ts}}. It is
# re-derived from live truth every render, so unlike a cache it can't drift --
# a wrong entry self-corrects on the next render. A gap longer than
# `_SESSION_DEBOUNCE_GAP_SECONDS` since the last elevated observation means the
# previous episode's clearing render was missed (lazy refresh), so we treat
# the new observation as a fresh episode and re-arm rather than trust a stale
# "first" stamp.
_SESSION_DEBOUNCE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-session-debounce.json"
)
_SESSION_DEBOUNCE_DWELL_SECONDS = 20
_SESSION_DEBOUNCE_GAP_SECONDS = 30
_SESSION_DEBOUNCE_MAX_AGE_SECONDS = 86400  # prune entries older than a day


def _load_debounce_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_debounce_state(path, state, now):
    pruned = {
        k: v
        for k, v in state.items()
        if isinstance(v, dict)
        and (now - v.get("last", 0)) <= _SESSION_DEBOUNCE_MAX_AGE_SECONDS
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except OSError:
        # Best-effort cache write; an unwritable cache dir is non-fatal.
        pass


def debounce_session_count(
    raw_count,
    cwd,
    *,
    now=None,
    state_path=None,
    dwell_seconds=_SESSION_DEBOUNCE_DWELL_SECONDS,
    gap_seconds=_SESSION_DEBOUNCE_GAP_SECONDS,
):
    """Return the session count to *display*, suppressing brief restart blips.

    Reports `raw_count` unchanged once an elevated (>= 2) count has persisted
    for `dwell_seconds`; until then an elevated count is reported as 1 so the
    badge stays quiet. Counts below 2 pass straight through and clear any
    tracked episode. Returns `raw_count` unchanged when `cwd` is empty (no key
    to track state by). Never raises -- statusline rendering must not crash.
    """
    now = time.time() if now is None else now
    key = os.path.normcase(cwd or "")
    if not key:
        return raw_count
    path = state_path or _SESSION_DEBOUNCE_PATH
    state = _load_debounce_state(path)
    entry = state.get(key)

    if raw_count < 2:
        if entry is not None:
            state.pop(key, None)
            _save_debounce_state(path, state, now)
        return raw_count

    # raw_count >= 2: continue an in-progress episode, or start a fresh one.
    if not isinstance(entry, dict) or (now - entry.get("last", 0)) > gap_seconds:
        entry = {"first": now, "last": now}
    else:
        entry = {"first": entry.get("first", now), "last": now}
    state[key] = entry
    _save_debounce_state(path, state, now)

    if now - entry["first"] >= dwell_seconds:
        return raw_count
    return 1
