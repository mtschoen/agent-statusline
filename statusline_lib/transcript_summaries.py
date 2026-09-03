"""Whole-transcript summaries, served from memory and recomputed on the pool.

Three render paths wanted a summary of an entire JSONL transcript: the beacon
column's anchor scan, each Agent Teams teammate's cost, and each subagent row's
context and cost. Every one of them read the whole file inline, and inside the
resident server "inline" is the single receive thread that answers every render
on the machine, so one large transcript slowed every session at once.

This module is the seam that keeps those reads off that thread. A summary is
whatever a refresher computes for one (kind, path); the render asks for it with
summary_for, which answers only out of the process-wide table and never opens
the transcript itself. A miss or a changed file hands recomputation to the
resident server's worker pool through server_jobs.request_refresh, exactly as
gitref.py and beacon_cache.py hand off their own lookups.

The stale-while-revalidate contract, per lookup:

  no entry, and a pool is there to ask   -- None now, a job submitted, and the
                                            next render has the value
  no entry, and no pool exists           -- computed on the calling thread, so
                                            a verify script or any process that
                                            is not the resident server still
                                            renders correct numbers
  entry whose signature still matches    -- served as is
  entry whose file has changed since     -- the stale value served immediately,
                                            plus a job once the entry is at
                                            least the minimum refresh interval
                                            old, so a transcript being appended
                                            to on every render does not queue a
                                            walk on every render

A signature is (size, mtime_ns) from one os.stat, which is the whole cost the
receive thread pays. Each refresher takes that signature BEFORE it reads the
file, so a write landing during the read leaves a signature that no longer
matches and the next lookup asks for another pass rather than trusting a walk
of a file that changed underneath it.

Imports:
  beacon_anchors -- _scan_beacon_anchors, the beacon-anchors refresher's work
  cost           -- walk_transcript, the transcript-walk refresher's work
  server_jobs    -- has_refresh_sink, request_refresh, run_refresh
  server_socket  -- pref_number, the same prefs seam server.py reads its own
                    constants through
"""

import os
import threading
import time

from .beacon_anchors import _scan_beacon_anchors
from .cost import walk_transcript
from .server_jobs import has_refresh_sink, request_refresh, run_refresh
from .server_socket import pref_number

# How many summaries the table holds before the least recently served ones are
# dropped. One entry per live transcript is the working set, and a machine with
# hundreds of live transcripts has bigger problems, so this is a ceiling on a
# leak rather than a tuning knob: the server outlives every session it serves,
# and without a bound its table would grow for the life of the process.
TRANSCRIPT_SUMMARY_MAXIMUM_ENTRIES = 256

# The floor on how often one path's summary is recomputed. A transcript grows
# on every turn, so without it an actively rendering session would queue a walk
# per render and the pool would spend its four threads re-walking one file.
TRANSCRIPT_SUMMARY_MINIMUM_REFRESH_SECONDS = 5

_MAXIMUM_ENTRIES_PREF = "STATUSLINE_TRANSCRIPT_SUMMARY_ENTRIES"
_MINIMUM_REFRESH_PREF = "STATUSLINE_TRANSCRIPT_SUMMARY_REFRESH_SECONDS"

# (kind, path) -> {"signature", "value", "computed_at", "served_at"}. The pool
# writes from its worker threads while the receive thread reads, so every touch
# of this dict happens under the lock beside it.
_SUMMARIES = {}
_SUMMARIES_LOCK = threading.Lock()


def _maximum_entries():
    return pref_number(_MAXIMUM_ENTRIES_PREF, TRANSCRIPT_SUMMARY_MAXIMUM_ENTRIES, int)


def _minimum_refresh_seconds():
    return pref_number(
        _MINIMUM_REFRESH_PREF, float(TRANSCRIPT_SUMMARY_MINIMUM_REFRESH_SECONDS), float
    )


def _signature(path):
    """(size, mtime_ns) for `path`, or None when it cannot be stat'd: a missing
    file, a path the process may not look at, or an empty string. None means
    there is nothing to summarize and nothing to ask the pool for."""
    try:
        status = os.stat(path)
    except (OSError, ValueError):
        return None
    return (status.st_size, status.st_mtime_ns)


def _now(now):
    return time.time() if now is None else now


def _store(kind, path, signature, value, now):
    """Publish one computed summary and shed the oldest entries above the
    ceiling. Called from the worker pool, so it holds the lock for exactly the
    dict work and nothing else."""
    maximum = _maximum_entries()
    with _SUMMARIES_LOCK:
        _SUMMARIES[(kind, path)] = {
            "signature": signature,
            "value": value,
            "computed_at": now,
            "served_at": now,
        }
        while len(_SUMMARIES) > maximum:
            oldest = min(_SUMMARIES, key=lambda key: _SUMMARIES[key]["served_at"])
            del _SUMMARIES[oldest]
    return value


def summary_for(kind, path, now=None):
    """The summary of `kind` for `path`, or None when there is not one yet.

    Never opens the transcript: one os.stat is the whole cost on the caller's
    thread. None is a real answer and every consumer renders without the field
    rather than waiting for it, because the pool fills the value in and the
    next render a moment later has it. `now` is injected by tests and defaults
    to the wall clock.
    """
    signature = _signature(path)
    if signature is None:
        return None
    now = _now(now)
    with _SUMMARIES_LOCK:
        entry = _SUMMARIES.get((kind, path))
        if entry is not None:
            entry["served_at"] = now
            value = entry["value"]
            matches = entry["signature"] == signature
            age = now - entry["computed_at"]
    if entry is not None:
        if not matches and age >= _minimum_refresh_seconds():
            request_refresh(kind, path)
        return value
    if has_refresh_sink():
        request_refresh(kind, path)
        return None
    return _compute_inline(kind, path)


def _compute_inline(kind, path):
    """Compute one summary on the calling thread, for every process that is not
    the resident server. Those have no pool to hand the work to, so a render
    that answered None here would never have a value at all. An unreadable
    transcript is None, the same answer a path that cannot be stat'd gets."""
    try:
        return run_refresh(kind, path)
    except OSError:
        return None


def refresh_transcript_walk(path, now=None):
    """Recompute `path`'s cost/token walk and publish it for the render's
    lookup. Runs on the resident server's worker pool (server_jobs.run_refresh)
    or, in a process with no pool, on the render's own thread."""
    signature = _signature(path)
    value = walk_transcript(path, include_subagents=False)
    return _store("transcript-walk", path, signature, value, _now(now))


def refresh_beacon_anchors(path, now=None):
    """Recompute `path`'s beacon anchor state and publish it for the render's
    lookup. Same contract as refresh_transcript_walk; the scan itself lives in
    beacon_anchors.py."""
    signature = _signature(path)
    value = _scan_beacon_anchors(path)
    return _store("beacon-anchors", path, signature, value, _now(now))


def reset_transcript_summaries():
    """Empty the table. The server calls this on close so a table does not
    outlive the process's role as a server, and tests call it so one check's
    entries are never another check's warm cache."""
    with _SUMMARIES_LOCK:
        _SUMMARIES.clear()
