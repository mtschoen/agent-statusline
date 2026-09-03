"""Session counting and debounce helpers.

Detects supported agent sessions running in the same cwd so the
statusline can warn that a second interactive instance is active here.

Enumerates Claude, Qwen, and Kimi runtime processes whose own cwd matches,
which are not in
`-p` headless mode (scripted runs), which don't carry Claude Code's own
child-session environment marker (`_is_child_session_env` -- a subagent's
tool-execution process, ground truth straight from the harness, no
ancestry needed), and which pass the process-tree test in
`_is_excluded_by_tree`: a real session is launched by a live shell, so a
an agent descendant of another agent runtime (update check, helper, spawned
agent runtime) or an agent whose launching parent is dead (disowned helper,
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
from .process_snapshot import _LazySnapshot, _resolve_psutil

_SESSION_COUNT_CACHE_FILENAME = ".statusline-sessioncount-cache.json"
_SESSION_COUNT_CACHE_MAX_AGE_SECONDS = 86400  # prune entries older than a day
_CACHE_ENTRY_UNSET = object()
_TEXT_ENCODING = "utf-8"


def session_count_cache_path():
    return os.path.join(app_dir(), _SESSION_COUNT_CACHE_FILENAME)


def load_session_count_entry(cwd, *, cache_path=None):
    if not cwd:
        return None
    path = cache_path or session_count_cache_path()
    entry = _load_session_count_cache(path).get(os.path.normcase(cwd))
    return entry if isinstance(entry, dict) else None


def _load_session_count_cache(path):
    try:
        with open(path, encoding=_TEXT_ENCODING) as f:
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
        with open(path, "w", encoding=_TEXT_ENCODING) as f:
            json.dump(pruned, f)
    except OSError:
        # Best-effort cache write; an unwritable/missing cache dir is non-fatal
        # and must never break statusline rendering.
        pass


def count_active_sessions(cwd, *, cache_path=None, cache_entry=_CACHE_ENTRY_UNSET):
    """Return how many supported interactive agent sessions run in `cwd` --
    whatever the cache holds, stale included, never a synchronous psutil
    scan.

    Render-perf ratchet step 3 (PLAN.md): a cold psutil process-tree walk
    measured ~120ms on a machine with a few hundred processes, well past the
    <10ms warm-core budget, uncached. So this is a pure read: any entry is
    served as-is, and a missing one reads 0, the same honest degrade as a
    machine without psutil.

    Nothing here judges the entry's age, because nothing here can act on the
    answer. Recomputation belongs to the resident server, the only process
    that knows the whole set of live directories -- and since one process
    walk answers every directory at once, it schedules that walk for the
    whole set rather than reacting to whichever render noticed a stale entry
    first. Returns 0 immediately when `cwd` is empty -- no key to cache by.
    Never raises -- statusline rendering must not crash.
    """
    if not cwd:
        return 0
    if cache_entry is _CACHE_ENTRY_UNSET:
        cache_entry = load_session_count_entry(cwd, cache_path=cache_path)
    if isinstance(cache_entry, dict):
        return int(cache_entry.get("count", 0))
    return 0


def refresh_session_count_cache(cwds, *, cache_path=None):
    """Recompute the active-session count for `cwds` -- one directory or a
    sequence of them -- and persist every result for the render's cached
    read. Runs on the resident server's worker pool (server_jobs.run_refresh),
    never on the render path.

    The psutil walk is machine-wide, so the whole set is scored against ONE
    process snapshot: six live directories cost one walk, not six. Returns
    the count written for the last directory scored (the only one, for the
    single-string call every pre-server caller made), and 0 when psutil is
    unavailable or nothing was handed in -- the cache is still written
    either way, so the render serves an honest 0 rather than a stale count.
    """
    now = time.time()
    path = cache_path or session_count_cache_path()
    targets = [cwds] if isinstance(cwds, str) else list(cwds)
    _reset_process_scan_counter()
    if not targets:
        # The server hands over whatever its cwd table holds, which is empty
        # until the first render and again after the last session is dropped.
        # A machine-wide walk that answers nobody is pure waste.
        return 0
    psutil = _resolve_psutil()
    scan = None if psutil is None else _ProcessScan(psutil)
    cache = _load_session_count_cache(path)
    count = 0
    for target in targets:
        count = 0 if scan is None else _count_one(target, psutil, scan)
        cache[os.path.normcase(target)] = {"count": count, "ts": now}
    _save_session_count_cache(path, cache, now)
    return count


def _count_one(target_cwd, psutil, scan):
    """`target_cwd`'s count from an already-taken process scan, degrading to
    0 rather than propagating out of the worker pool and costing every other
    directory in the same refresh its result."""
    try:
        return _count_via_psutil(target_cwd, psutil, scan)
    except Exception:
        return 0


_AGENT_PROCESS_NAMES = ("claude", "claude.exe", "qwen", "qwen.exe")
_NODE_PROCESS_NAMES = ("node", "node.exe")


def _is_direct_agent_runtime(name):
    n = (name or "").lower()
    if n in _AGENT_PROCESS_NAMES:
        return True
    kimi_name = "kimi.exe" if os.name == "nt" else "kimi"
    return n == kimi_name


def _is_agent_runtime(name, cmdline):
    """Pure classifier: does (name, cmdline) look like a supported agent
    runtime process at all, either a bare binary or node wrapping a CLI?
    Cwd and headless flags are deliberately out of scope: this is also applied
    to ancestors, where those don't matter."""
    n = (name or "").lower()
    if _is_direct_agent_runtime(n):
        return True
    if n in ("node", "node.exe"):
        runtime_markers = ("claude", "qwen")
        if os.name != "nt":
            runtime_markers += ("kimi",)
        return any(
            any(marker in (argument or "").lower() for marker in runtime_markers)
            for argument in (cmdline or ())
        )
    return False


# Parent chains on a healthy box are a handful of shells deep; the cap only
# guards against pathological/cyclic ppid data.
_ANCESTOR_WALK_LIMIT = 15


def _is_excluded_by_tree(pid, snap, cmdline_of):
    """Pure classifier: is candidate `pid` structurally NOT an interactive
    session, judged against `snap` ({pid: (ppid, name, create_time)}, one
    process-table snapshot)?

    Two structural rules, no clocks:
      - Agent-descendant: anything whose ancestor chain contains a supported
        agent runtime was spawned BY a session (update check, helper, runtime)
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
    treated as non-agent, which at worst counts a session that could have been
    excluded, and never hides a real one.
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
    while cur not in seen and len(seen) < _ANCESTOR_WALK_LIMIT:
        row = snap.get(cur)
        if row is None:
            break  # chain breaks above the first ancestor: ends the walk
        seen.add(cur)
        next_ppid, name, pctime = row
        if (pctime or 0) > (child_ctime or 0):
            break  # recycled pid above the first ancestor: chain ends here
        n = (name or "").lower()
        if _is_direct_agent_runtime(n):
            return True
        if n in _NODE_PROCESS_NAMES and _is_agent_runtime(n, cmdline_of(cur)):
            return True
        child_ctime = pctime
        cur = next_ppid
    return False


def _process_matches(name, cmdline, cwd, target_cwd):
    """Pure classifier: does this (name, cmdline, cwd) tuple represent an
    supported interactive agent session rooted at `target_cwd`? Extracted so
    unit tests don't need a live or mocked psutil."""
    if not _is_agent_runtime(name, cmdline):
        return False
    cl = cmdline or ()
    if "-p" in cl or "--print" in cl:
        return False
    if not cwd:
        return False
    return os.path.normcase(cwd) == os.path.normcase(target_cwd)


# Claude Code sets this on processes it spawns to run a subagent's own tool
# calls (issue #11: a Task-tool subagent sharing the parent's cwd was
# tripping the [N sessions] badge). Verified empirically 2026-07-12: a live
# Task-tool subagent's own Bash-tool child process carries
# CLAUDE_CODE_CHILD_SESSION=1 in its environment while the shared top-level
# `claude.exe` process it runs inside (and genuine independent sessions) do
# not. This is authoritative straight from the harness -- no ancestry
# needed -- so it catches shapes the process-tree walk can't see through
# (a detached/relaunched spawn whose immediate parent isn't the session
# that launched it).
_CHILD_SESSION_ENV_VAR = "CLAUDE_CODE_CHILD_SESSION"
_FALSY_ENV_VALUES = ("", "0", "false", "False")


def _is_child_session_env(env):
    """Pure classifier: does `env` (a process environment mapping, or None
    when unreadable) carry Claude Code's child-session marker?

    None/empty/falsy values are never a marker -- unreadable environ() (e.g.
    AccessDenied) must fail OPEN here (not excluded), matching the rest of
    this module's philosophy: at worst count a session that could have been
    excluded, never hide a real one.
    """
    if not env:
        return False
    return env.get(_CHILD_SESSION_ENV_VAR, "") not in _FALSY_ENV_VALUES


# How many machine-wide process scans the current refresh has taken. A
# diagnostic counter only, never read for control flow, so the worker pool
# running two refreshes at once can interleave it harmlessly. One walk
# answers every live directory, so a refresh of any number of them must
# report 1; the verify suite reads this to hold that invariant mechanically
# rather than by inspection.
_process_scans_taken = 0


def process_snapshots_taken():
    """Process scans taken since the current refresh began."""
    return _process_scans_taken


def _reset_process_scan_counter():
    global _process_scans_taken
    _process_scans_taken = 0


class _ProcessScan:
    """One pass over the machine's process table, reusable across
    directories.

    Nothing in the enumeration depends on which directory is being counted:
    the same candidate set and the same ancestor rows answer every one of
    them. So the resident server takes this once per refresh and scores
    every live directory against it, instead of walking a few hundred
    processes once per directory. Constructing one is what
    process_snapshots_taken counts.
    """

    def __init__(self, psutil):
        global _process_scans_taken
        _process_scans_taken += 1
        # The enumeration pass must stay attrs=["name"]: names come from one
        # toolhelp snapshot, while ppid/create_time force an OpenProcess per
        # pid on Windows (measured 20ms vs 11s over ~600 processes). The tree
        # walk gets those lazily from _LazySnapshot for the few pids it
        # visits, which also avoids per-ancestor cmdline() calls -- those
        # AccessDenied mid-chain and silently truncate the walk.
        self.names = {}
        self.candidates = []
        for p in psutil.process_iter(["name"]):
            self.names[p.pid] = p.info.get("name")
            name = (p.info.get("name") or "").lower()
            # Cheap name pre-filter -- avoids calling cmdline()/cwd() on
            # every process (hundreds on a typical box).
            if _is_direct_agent_runtime(name) or name in _NODE_PROCESS_NAMES:
                self.candidates.append((p.pid, name, p))
        self.rows = _LazySnapshot(psutil, self.names)


def _count_via_psutil(target_cwd, psutil, scan=None):
    """How many interactive agent sessions `scan` shows rooted at
    `target_cwd`, taking a fresh process scan when none is handed in."""
    if scan is None:
        scan = _ProcessScan(psutil)
    snap = scan.rows

    def cmdline_of(pid):
        try:
            return psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    count = 0
    for pid, name, p in scan.candidates:
        try:
            cmdline = p.cmdline()
            if not _is_agent_runtime(name, cmdline):
                continue
            pcwd = p.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not _process_matches(name, cmdline, pcwd, target_cwd):
            continue
        # environ() is only worth the syscall once name/cmdline/cwd already
        # matched. Unreadable -> None -> _is_child_session_env fails open
        # (not excluded), same as the tree walk's AccessDenied handling.
        try:
            env = p.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            env = None
        if _is_child_session_env(env):
            continue
        if not _is_excluded_by_tree(pid, snap, cmdline_of):
            count += 1
    return count


# `count_active_sessions` reports live process truth, but a restart produces a
# brief handoff overlap: the old agent process is still winding down when
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
        with open(path, encoding=_TEXT_ENCODING) as f:
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
        with open(path, "w", encoding=_TEXT_ENCODING) as f:
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
