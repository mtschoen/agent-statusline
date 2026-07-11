"""Session counting and debounce helpers.

Detects other Claude Code sessions running in the same cwd so the
statusline can warn that a second interactive instance is active here.

Enumerates `claude` processes whose own cwd matches, which are not in
`-p` headless mode (scripted runs), and which pass the process-tree test
in `_is_excluded_by_tree`: a real session is launched by a live shell, so
a claude descendant of a claude (update check, helper, spawned agent
runtime) or a claude whose launching parent is dead (disowned helper,
dead-terminal zombie) is never an independent session no matter how long
it lives -- structural truth where a time-based dwell can't work. Ground
truth -- catches idle sessions, ignores ones that cleanly /exit'd a
moment ago. Requires
`psutil`; without it the badge stays off entirely (any mtime-based
substitute false-positives for ~5 minutes after a clean /exit, which the
20s restart-handoff debounce can't suppress).
"""

import json
import os
import time

from .base import app_dir

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
    app_dir(), ".statusline-sessioncount-cache.json"
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


def _is_agent_runtime(name, cmdline):
    """Pure classifier: does (name, cmdline) look like a claude/qwen runtime
    process at all -- a bare binary, or node wrapping the CLI? Cwd and headless
    flags are deliberately out of scope: this is also applied to *ancestors*,
    where those don't matter."""
    n = (name or "").lower()
    if n in ("claude", "claude.exe", "qwen", "qwen.exe"):
        return True
    if n in ("node", "node.exe"):
        return any(
            "claude" in (arg or "").lower() or "qwen" in (arg or "").lower()
            for arg in (cmdline or ())
        )
    return False


# Parent chains on a healthy box are a handful of shells deep; the cap only
# guards against pathological/cyclic ppid data.
_ANCESTOR_WALK_LIMIT = 15

_AGENT_PROCESS_NAMES = ("claude", "claude.exe", "qwen", "qwen.exe")
_NODE_PROCESS_NAMES = ("node", "node.exe")


def _is_excluded_by_tree(pid, snap, cmdline_of):
    """Pure classifier: is candidate `pid` structurally NOT an interactive
    session, judged against `snap` ({pid: (ppid, name, create_time)}, one
    process-table snapshot)?

    Two structural rules, no clocks:
      - Agent-descendant: anything whose ancestor chain contains a claude/qwen
        runtime was spawned BY a session (update check, helper, agent runtime)
        and is never an independent session.
      - Orphan: a real session's launching shell stays alive (it's the user's
        terminal). A candidate whose immediate parent is dead, recycled
        (create_time newer than the child's), or init/pid-1 (Unix reparenting)
        is a disowned helper or a dead-terminal zombie -- either way not a
        session anyone is interacting with.

    A chain that breaks ABOVE a live first ancestor ends the walk without
    excluding (observed live: a real session whose terminal host had exited
    while its shell survived). `cmdline_of(pid) -> list | None` is only
    consulted to classify node-named ancestors; None means unreadable and is
    treated as non-agent, at worst reproducing the old overcount, never hiding
    a real session.
    """
    row = snap.get(pid)
    if row is None:
        return True  # exited mid-scan; nothing to count
    ppid, _name, ctime = row
    parent = snap.get(ppid)
    if parent is None or ppid in (0, 1) or (parent[2] or 0) > (ctime or 0):
        return True  # orphan
    seen = set()
    child_ctime = ctime
    cur = ppid
    while cur in snap and cur not in seen and len(seen) < _ANCESTOR_WALK_LIMIT:
        seen.add(cur)
        next_ppid, name, pctime = snap[cur]
        if (pctime or 0) > (child_ctime or 0):
            break  # recycled pid above the first ancestor: chain ends here
        n = (name or "").lower()
        if n in _AGENT_PROCESS_NAMES:
            return True
        if n in _NODE_PROCESS_NAMES and _is_agent_runtime(n, cmdline_of(cur)):
            return True
        child_ctime = pctime
        cur = next_ppid
    return False


def _process_matches(name, cmdline, cwd, target_cwd):
    """Pure classifier: does this (name, cmdline, cwd) tuple represent an
    interactive Claude/Qwen session rooted at `target_cwd`? Extracted so unit
    tests don't need a live or mocked psutil."""
    if not _is_agent_runtime(name, cmdline):
        return False
    cl = cmdline or ()
    if "-p" in cl or "--print" in cl:
        return False
    if not cwd:
        return False
    return os.path.normcase(cwd) == os.path.normcase(target_cwd)


def _count_via_psutil(target_cwd, psutil):
    # One pass over the process table builds both the pid->(ppid, name,
    # create_time) snapshot the tree walk needs and the candidate list. The
    # walk reads names from the snapshot rather than calling per-ancestor
    # psutil APIs -- those AccessDenied mid-chain and silently truncate.
    snap = {}
    candidates = []
    for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        info = p.info
        snap[info.get("pid")] = (
            info.get("ppid"),
            info.get("name"),
            info.get("create_time"),
        )
        name = (info.get("name") or "").lower()
        # Cheap name pre-filter -- avoids calling cmdline()/cwd() on every
        # process (hundreds on a typical box).
        if name in _AGENT_PROCESS_NAMES or name in _NODE_PROCESS_NAMES:
            candidates.append((info.get("pid"), name, p))

    def cmdline_of(pid):
        try:
            return psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    count = 0
    for pid, name, p in candidates:
        try:
            cmdline = p.cmdline()
            pcwd = p.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if _process_matches(
            name, cmdline, pcwd, target_cwd
        ) and not _is_excluded_by_tree(pid, snap, cmdline_of):
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
_SESSION_DEBOUNCE_PATH = os.path.join(app_dir(), ".statusline-session-debounce.json")
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
