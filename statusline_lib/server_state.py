"""Per-session incremental transcript state for the resident server.

Re-walking a session's entire transcript per render
(statusline_lib.cost.walk_transcript) would cost the resident server that
whole walk on every datagram, so this module keeps one SessionEntry per
session id in memory and folds only the transcript bytes appended since the
previous render (read_appended), composing the seam Task 7 split out of
cost.py (new_walk_accumulator, fold_transcript_lines, summarize_walk) one
append at a time rather than calling walk_transcript's single-shot version.

The state directory also accumulates per-session cache files that nothing
sweeps while no process outlives the render that wrote them. The resident
server does outlive them, so
housekeep_state_dir sweeps files matching HOUSEKEEPING_PREFIXES that have
aged past HOUSEKEEPING_MAX_AGE_SECONDS, on start and every
HOUSEKEEPING_INTERVAL_SECONDS after that.

Imports:
  cost -- new_walk_accumulator, fold_transcript_lines, summarize_walk (the
          accumulator seam; re-exported from cost.py, defined in
          cost_walk.py)
"""

import glob
import os
import time

from .cost import fold_transcript_lines, new_walk_accumulator, summarize_walk

HOUSEKEEPING_MAX_AGE_SECONDS = 7 * 86400
HOUSEKEEPING_INTERVAL_SECONDS = 3600
HOUSEKEEPING_PREFIXES = ("beacons-latest-", "last-render-", "render-timer-")

# How long an entry survives with no render referencing it. A session that
# has not rendered for an hour has been closed or abandoned, and a working
# directory no live session points at has nothing left to refresh, so both
# are dropped rather than carried for the life of the server process.
SESSION_DROP_SECONDS = 3600
CWD_DROP_SECONDS = 3600


def housekeep_state_dir(directory, now, max_age_seconds=HOUSEKEEPING_MAX_AGE_SECONDS):
    """Delete per-session state files older than `max_age_seconds`, returning
    how many went.

    Scoped to HOUSEKEEPING_PREFIXES: these are per-session caches that no
    longer have a session, and nothing reads a week-old one. Every other file
    in the state directory, the server info file included, is left alone.
    Best effort throughout: an unreadable directory or an undeletable file
    costs the sweep, never the server.
    """
    deleted = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(HOUSEKEEPING_PREFIXES):
            continue
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            os.remove(path)
        except OSError:
            continue
        deleted += 1
    return deleted


def read_appended(path, offset):
    """Return (complete_lines, new_offset, rewalked) for `path` beyond
    `offset`.

    Only bytes up to the last newline are consumed, so a transcript caught
    mid-write leaves its partial trailing line for the next call rather than
    folding half a JSON object. A file that has shrunk below `offset` was
    rewritten under us, so it is rewalked from zero and `rewalked` is True.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset, False
    rewalked = False
    if size < offset:
        offset = 0
        rewalked = True
    if size == offset:
        return [], offset, rewalked
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return [], offset, rewalked
    consumed = chunk.rfind(b"\n") + 1
    if consumed == 0:
        return [], offset, rewalked
    text = chunk[:consumed].decode("utf-8", "replace")
    # Split on "\n" only, not str.splitlines()'s full Unicode line-boundary
    # set (which also breaks on \v, \f, \x1c-\x1e, \x85, U+2028, U+2029).
    # JSON allows those raw inside a string, and walk_transcript's own
    # text-mode file iteration never treats them as line breaks either; a
    # trailing \r from a CRLF-terminated line is harmless JSON whitespace.
    # `text` always ends with exactly one "\n" (consumed is built from
    # rfind(b"\n") + 1), so the split's last element is always "".
    return text.split("\n")[:-1], offset + consumed, rewalked


def _drop_stale(table, now, drop_seconds):
    """Delete every entry in `table` unseen for `drop_seconds`, returning how
    many went. Keys are collected before deleting: a dict cannot be mutated
    while it is being iterated."""
    stale = [
        key for key, entry in table.items() if now - entry.last_seen >= drop_seconds
    ]
    for key in stale:
        del table[key]
    return len(stale)


class SessionEntry:
    """One session's incremental walk state: the running accumulator, the
    dedup set of assistant message ids it has folded, one byte offset per
    transcript file (parent plus each subagent), and the parent/subagent
    cost split needed to reproduce walk_transcript's summary."""

    def __init__(self, session_id, transcript_path, clock):
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.offsets = {}
        self.accumulator = new_walk_accumulator()
        self.seen_ids = set()
        self.parent_cost = 0.0
        self.last_seen = clock()
        self.rewalk_count = 0


class CwdEntry:
    """One live working directory's lifetime record: its name and when a
    render last mentioned it.

    Deliberately nothing else. Everything a cwd stands for -- git ref,
    working-tree badge, session count -- already lives in its own on-disk
    TTL cache that the render reads directly, so duplicating those values
    here would only create a second copy to keep honest. What the server
    needs, and cannot get from those caches, is which directories are still
    live: it refreshes a directory only while a session references it, and
    stops CWD_DROP_SECONDS after the last one goes.
    """

    def __init__(self, cwd, clock):
        self.cwd = cwd
        self.last_seen = clock()


class StateTables:
    """The resident server's in-memory tables: one per session, one per
    working directory. One StateTables instance lives for the lifetime of
    the server process; sessions are looked up by id and never rewalked from
    scratch except when their transcript file shrinks or changes out from
    under them, and both tables shed entries no render has referenced for
    their drop window."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._sessions = {}
        self._cwds = {}
        self._rewalk_count = 0

    def touch_session(self, session_id, transcript_path):
        """Return the SessionEntry for `session_id`, creating one on first
        use. Refreshes last_seen and the recorded transcript path on every
        call, since a resumed session can change which file it writes to."""
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = SessionEntry(session_id, transcript_path, self._clock)
            self._sessions[session_id] = entry
        else:
            if entry.transcript_path != transcript_path:
                # The session id now points at a different file. Its turns
                # are already in this entry's accumulator under the old
                # path's offsets, and folding a second file on top would
                # add the new file's cost to the old file's totals. Same
                # remedy as a transcript that shrank: start clean.
                self._reset(entry)
            entry.transcript_path = transcript_path
            entry.last_seen = self._clock()
        return entry

    def touch_cwd(self, cwd):
        """Return the CwdEntry for `cwd`, creating one on first use, and
        stamp it as seen now. Keys are normcased so case-equivalent Windows
        paths share one refresh row."""
        key = os.path.normcase(cwd)
        entry = self._cwds.get(key)
        if entry is None:
            entry = CwdEntry(key, self._clock)
            self._cwds[key] = entry
        else:
            entry.last_seen = self._clock()
        return entry

    def known_cwds(self):
        """Every live working directory, in first-seen order. The server
        hands this whole list to one session-count refresh, because a single
        machine-wide process walk already answers all of them."""
        return list(self._cwds)

    def drop_idle(self):
        """Delete every entry unseen for its drop window, returning
        (sessions_dropped, cwds_dropped).

        This is what bounds a server process that outlives every session it
        serves: without it both tables, and the refresh schedule built from
        the cwd table, would grow with every directory the machine has ever
        rendered in.
        """
        now = self._clock()
        return (
            _drop_stale(self._sessions, now, SESSION_DROP_SECONDS),
            _drop_stale(self._cwds, now, CWD_DROP_SECONDS),
        )

    def summary(self):
        """What the `status` request kind reports: current table sizes and
        the server-lifetime number of session fold resets. The rewalk total is
        monotonic even when the session that incurred a reset is evicted."""
        return {
            "sessions": len(self._sessions),
            "cwds": len(self._cwds),
            "rewalks": self._rewalk_count,
        }

    def walk_for(self, entry):
        """Fold whatever has been appended since the last call and return the
        session's totals.

        A rewalk (any file, parent or subagent, shrunk below its recorded
        offset) invalidates the whole session's fold state, not just the one
        file that shrank: the shared accumulator and seen_ids set already
        hold that file's old contribution, and re-folding its content from
        zero on top of that would double-count it. So every file is read
        once to detect a rewalk before anything is folded, and a rewalk
        anywhere resets the entire entry and re-reads every file from zero.
        """
        subagent_paths = self._subagent_paths(entry.transcript_path)
        parent_lines, parent_offset, parent_rewalked = read_appended(
            entry.transcript_path, entry.offsets.get(entry.transcript_path, 0)
        )
        subagent_reads = [
            (path, *read_appended(path, entry.offsets.get(path, 0)))
            for path in subagent_paths
        ]
        if parent_rewalked or any(rewalked for *_, rewalked in subagent_reads):
            self._reset(entry)
            parent_lines, parent_offset, _ = read_appended(entry.transcript_path, 0)
            subagent_reads = [
                (path, *read_appended(path, 0)) for path in subagent_paths
            ]
        accumulator = entry.accumulator
        entry.offsets[entry.transcript_path] = parent_offset

        accumulator["track_evictions"] = True
        accumulator["track_user_prompts"] = True
        before = accumulator["cost"]
        fold_transcript_lines(parent_lines, accumulator, entry.seen_ids)
        entry.parent_cost += accumulator["cost"] - before

        accumulator["track_evictions"] = False
        accumulator["track_user_prompts"] = False
        for path, lines, offset, _ in subagent_reads:
            entry.offsets[path] = offset
            fold_transcript_lines(lines, accumulator, entry.seen_ids)

        entry.last_seen = self._clock()
        return summarize_walk(accumulator, entry.parent_cost)

    def _reset(self, entry):
        """Replace an entry's fold state entirely: the transcript was
        rewritten out from under us (shrank below its last known offset), so
        anything already folded is now untrustworthy."""
        entry.accumulator = new_walk_accumulator()
        entry.seen_ids = set()
        entry.offsets = {}
        entry.parent_cost = 0.0
        entry.rewalk_count += 1
        self._rewalk_count += 1

    def _subagent_paths(self, transcript_path):
        """The same <path-without-.jsonl>/subagents/agent-*.jsonl rule
        walk_transcript uses, so which files count is unchanged."""
        if not transcript_path.endswith(".jsonl"):
            return []
        subagent_directory = transcript_path[:-6] + "/subagents"
        return glob.glob(os.path.join(subagent_directory, "agent-*.jsonl"))
