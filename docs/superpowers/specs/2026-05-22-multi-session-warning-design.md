# Multi-session warning on the statusline

**Date:** 2026-05-22
**Status:** Approved, ready for plan

## Problem

When the user opens a second Claude Code session in a folder that already has
one running, there's no signal that the conflict exists. Both sessions read
and write the same files, which can lead to silent overwrites, confusing
diffs, and lost work. The statusline is the natural place to surface this:
it's visible at the start of every session and updates on every prompt.

## Goal

When two or more Claude Code sessions are recently active in the current
working directory, the statusline shows a red `[N sessions]` label between
the cwd path and the git branch on line 1. The label is loud enough to
register at session start (the moment when the user can still abort), and
disappears on its own once the other sessions go idle.

## Detection

### Source of truth

Claude Code writes one JSONL transcript per session to
`~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`. Subagent transcripts
live in sibling subdirectories (`<session-uuid>/agent-*.jsonl`), not at the
top level. Every tool call within a session appends to the parent JSONL,
so its `mtime` is a precise "this session did something recently" signal.

### Slug

The slug Claude Code uses for the current cwd is derivable from the path,
but the simplest robust source is the transcript path that's already on
the stdin payload — `d["transcript_path"]` points into the exact slug dir.
The helper takes the slug dir, not the cwd:

```python
slug_dir = os.path.dirname(transcript_path) if transcript_path else None
```

If the payload lacks `transcript_path` (older Claude Code, edge case), the
helper returns no decoration rather than guessing.

### Counting rule

- `os.scandir(slug_dir)` — top-level only, no recursion.
- Keep entries where `name.endswith(".jsonl")` and `entry.is_file()`.
- Keep entries where `now - entry.stat().st_mtime <= 300` (5 minutes).
- Return `len(matches)`.
- Self counts (the current session's JSONL gets touched every render).

### Display rule

- If count `>= 2`, render `[N sessions]` in red between cwd and `(branch)`.
- If count `< 2`, render nothing — no decoration when alone.

## UX

```
⠋ [chonkers] C:/Users/mtsch/schoen-claude-status [2 sessions] (main)
183.7K / 1.00M (18.0%) | 15.41M / 207.4K / 99% hit | $10.66
```

- Red: ANSI `\x1b[31m` … `\x1b[0m`, matching how the rest of the statusline
  emits color.
- Position: after cwd, before `(branch)`. If no branch is present, label
  goes at the end of line 1.
- Wording: `[N sessions]` — same noun whether N is 2 or 50. Keeps the
  format stable; no plural-vs-singular branch.

## Code shape

New helper in `statusline_lib.py`, called from `statusline.py:main()`:

```python
def count_active_sessions(transcript_path, now=None, window_seconds=300):
    """Return how many JSONL session transcripts in the same project dir
    have an mtime within the last `window_seconds`. Counts include the
    current session. Returns 0 on any error.
    """
```

`statusline.py:main()` calls it after resolving cwd/branch and splices
the result into `line1`:

```python
session_label = ""
n = count_active_sessions(d.get("transcript_path") or "")
if n >= 2:
    session_label = f" \x1b[31m[{n} sessions]\x1b[0m"
line1 = f"{spinner} [{_hostname()}] {cwd}{session_label}"
if branch:
    line1 = f"{line1} ({branch})"
```

## Failure modes

The statusline must never crash. The helper wraps its file operations in a
broad `try` and returns `0` on any `OSError` (missing dir, permission
denied, race with a session creating its JSONL, etc.). The caller's
`n >= 2` check then naturally yields no decoration.

## Out of scope

- Cross-folder detection. The label is per-cwd by design — sessions in
  other directories don't conflict.
- Process-based detection (enumerating running `claude.exe`). The mtime
  signal is good enough and far cheaper.
- Configurable thresholds. 300s is hardcoded; revisit if it proves wrong.
- Listing or identifying the other sessions. The count is enough to
  prompt the user to investigate.

## Testing

- Unit test on `count_active_sessions` with a `tmp_path` containing a
  varying mix of stale and fresh JSONLs plus subagent subdirs. Verify:
  - Stale-only → 0.
  - Two fresh → 2.
  - Subagent subdir contents are ignored.
  - Missing dir / empty path → 0 with no exception.
- No statusline-rendering test required; the splice is one line.
