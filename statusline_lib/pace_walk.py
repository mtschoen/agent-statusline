"""Transcript discovery and hourly spend aggregation for pace calculations."""

import os
from datetime import UTC, datetime

from .base import _json_loads
from .cost import _cost_for_turn


def _parse_pace_line(line, seen_ids, earliest):
    """Parse one JSONL line for the pace walk. Returns (ts, usage, model_id),
    or None to skip (blank, malformed, non-assistant, duplicate id, too old)."""
    if not line.strip():
        return None
    try:
        e = _json_loads(line)
    except Exception:
        return None
    msg = e.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    mid = msg.get("id")
    if mid and mid in seen_ids:
        return None
    ts_str = e.get("timestamp")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None
    if ts < earliest:
        return None
    # Only mark the id as seen once the line fully validates -- a truncated
    # line (e.g. missing timestamp) must not poison the id, or the later
    # complete copy of the same message gets dropped as a false duplicate.
    if mid:
        seen_ids.add(mid)
    return ts, (msg.get("usage") or {}), (msg.get("model") or "")


def _scandir_entries(dir_path):
    """os.scandir as a list; [] when the directory is missing or unreadable
    (a session dir without a subagents/ child is the everyday case)."""
    try:
        with os.scandir(dir_path) as it:
            return list(it)
    except OSError:
        return []


def _entry_in_window(entry, earliest):
    """True when the DirEntry's mtime is at/after `earliest`; unreadable -> False."""
    try:
        return entry.stat().st_mtime >= earliest
    except OSError:
        return False


def _discover_pace_groups(roots, earliest):
    """Group transcript files (parent jsonl + its subagents) by
    (slug, session_id), keeping only files whose mtime could hold in-range
    entries. The mtime prefilter prunes ~80% of files.

    Built on os.scandir rather than glob + os.path.getmtime: DirEntry.stat()
    reuses the directory listing's attributes (no per-file stat syscall on
    Windows, one round-trip per directory on network shares), where per-file
    getmtime cost seconds per render once roots grew to thousands of files.
    """
    groups = {}
    for proj_root in roots:
        for slug_entry in _scandir_entries(proj_root):
            if slug_entry.is_dir(follow_symlinks=False):
                _collect_session_files(slug_entry, earliest, groups)
    return groups


def _collect_session_files(slug_entry, earliest, groups):
    """One slug dir's contribution to the pace groups: parent JSONLs directly
    under the slug dir, plus each session dir's subagent JSONLs."""
    slug = slug_entry.name
    for entry in _scandir_entries(slug_entry.path):
        if entry.name.endswith(".jsonl"):
            if _entry_in_window(entry, earliest):
                session_id = entry.name[: -len(".jsonl")]
                groups.setdefault((slug, session_id), []).append(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            _collect_subagent_files(entry, slug, earliest, groups)


def _collect_subagent_files(session_entry, slug, earliest, groups):
    """agent-*.jsonl under `<session dir>/subagents`, grouped with the parent
    session's (slug, session_id) key."""
    for sub in _scandir_entries(os.path.join(session_entry.path, "subagents")):
        is_agent_jsonl = sub.name.startswith("agent-") and sub.name.endswith(".jsonl")
        if is_agent_jsonl and _entry_in_window(sub, earliest):
            groups.setdefault((slug, session_entry.name), []).append(sub.path)


def _pace_hourly_for_file(path, seen_ids, win_start_unix, n_buckets):
    """Per-file hourly $-burn list, length n_buckets, indexed from window start."""
    buckets = [0.0] * n_buckets
    last_model = ""
    try:
        with open(path, "rb") as f:
            for line in f:
                parsed = _parse_pace_line(line, seen_ids, earliest=win_start_unix)
                if parsed is None:
                    continue
                ts, usage, model_id = parsed
                if model_id:
                    last_model = model_id
                index = int((ts - win_start_unix) // 3600)
                if 0 <= index < n_buckets:
                    # Date prefix (UTC) for date-aware sonnet-5 pricing. The
                    # pace walk carries only the parsed epoch, so the date is
                    # derived from it rather than the raw ISO string; the two
                    # agree except within a sub-day timezone window around the
                    # 2026-09-01 boundary, immaterial to a live pace estimate.
                    date_prefix = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
                    buckets[index] += _cost_for_turn(
                        usage, model_id or last_model, date_prefix
                    )
    except OSError:
        return [0.0] * n_buckets
    return buckets


def _walk_session_hourly(paths, win_start_unix, n_buckets):
    """Hourly $-burn for one parent+subagents group. Module-level so a
    ProcessPoolExecutor can serialize it. Shared `seen_ids` across the group's
    files dedups the parent <-> auto-compact-subagent message.id overlap."""
    seen_ids = set()
    totals = [0.0] * n_buckets
    for path in paths:
        per_file = _pace_hourly_for_file(path, seen_ids, win_start_unix, n_buckets)
        for i in range(n_buckets):
            totals[i] += per_file[i]
    return totals


def _sum_hourly(into, addend):
    for i, value in enumerate(addend):
        into[i] += value


def _walk_hourly_inline(groups, win_start_unix, n_buckets):
    totals = [0.0] * n_buckets
    for paths in groups.values():
        _sum_hourly(totals, _walk_session_hourly(paths, win_start_unix, n_buckets))
    return totals


def _walk_hourly_parallel(groups, win_start_unix, n_buckets):
    workers = min(8, os.cpu_count() or 4)
    totals = [0.0] * n_buckets
    try:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_walk_session_hourly, paths, win_start_unix, n_buckets)
                for paths in groups.values()
            ]
            for fut in as_completed(futures):
                try:
                    _sum_hourly(totals, fut.result())
                except Exception:
                    # Worker failure: skip this group's contribution (it zeros out)
                    continue
    except (OSError, RuntimeError):
        return _walk_hourly_inline(groups, win_start_unix, n_buckets)
    return totals
