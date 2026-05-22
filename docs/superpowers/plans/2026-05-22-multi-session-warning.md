# Multi-session warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a red `[N sessions]` label on line 1 of the statusline whenever two or more Claude Code sessions are recently active in the current project directory, so the user notices the conflict at session start.

**Architecture:** A pure helper in `statusline_lib.py` counts `*.jsonl` files in the session's slug dir (derived from `transcript_path` on the stdin payload) whose mtime is within the last 300 seconds. The helper returns an integer (0 on any error). `statusline.py:main()` calls it after resolving cwd/branch and splices `[N sessions]` in `RED` between the cwd and the branch on line 1 when the count is ≥ 2. Subagent JSONLs live in subdirectories, so a top-level-only scan naturally excludes them.

**Tech Stack:** Python 3, stdlib `os.scandir`/`os.stat`, existing `RED`/`RESET` ANSI constants from `statusline_lib.py`. Verification follows the existing `scripts/verify_*.py` convention (stdlib `tempfile`, no pytest).

---

### Task 1: Add `count_active_sessions` helper to `statusline_lib.py`

**Files:**
- Modify: `statusline_lib.py` (append a new helper near the other module-level helpers)
- Create: `scripts/verify_active_session_count.py`

- [x] **Step 1: Write the verify script (failing)**

Create `scripts/verify_active_session_count.py`:

```python
"""Verify that `count_active_sessions` correctly counts recently-modified
session JSONLs in a project slug dir, ignoring subagent subdirectories
and stale files.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import count_active_sessions


def touch(path, mtime):
    with open(path, "w", encoding="utf-8") as f:
        f.write("{}\n")
    os.utime(path, (mtime, mtime))


def main():
    now = time.time()
    failures = []

    with tempfile.TemporaryDirectory() as slug_dir:
        transcript_path = os.path.join(slug_dir, "self.jsonl")

        # Case 1: empty dir -> 0
        if count_active_sessions(transcript_path, now=now) != 0:
            failures.append("empty dir should return 0")

        # Case 2: two fresh JSONLs -> 2
        touch(os.path.join(slug_dir, "self.jsonl"), now - 5)
        touch(os.path.join(slug_dir, "other.jsonl"), now - 60)
        if count_active_sessions(transcript_path, now=now) != 2:
            failures.append("two fresh JSONLs should return 2")

        # Case 3: stale file is ignored
        touch(os.path.join(slug_dir, "stale.jsonl"), now - 3600)
        if count_active_sessions(transcript_path, now=now) != 2:
            failures.append("stale file should be ignored")

        # Case 4: subagent subdir contents are ignored
        subagent_dir = os.path.join(slug_dir, "agent-uuid")
        os.makedirs(subagent_dir)
        touch(os.path.join(subagent_dir, "agent.jsonl"), now - 5)
        if count_active_sessions(transcript_path, now=now) != 2:
            failures.append("subagent subdir JSONL should be ignored")

        # Case 5: non-jsonl files are ignored
        touch(os.path.join(slug_dir, "scratch.txt"), now - 5)
        if count_active_sessions(transcript_path, now=now) != 2:
            failures.append("non-jsonl file should be ignored")

    # Case 6: missing dir / empty transcript path -> 0 without exception
    if count_active_sessions("", now=now) != 0:
        failures.append("empty transcript_path should return 0")
    if count_active_sessions("/nonexistent/path/foo.jsonl", now=now) != 0:
        failures.append("missing dir should return 0")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: count_active_sessions behaves correctly across all cases")


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Run script to verify it fails**

Run: `python C:/Users/mtsch/schoen-claude-status/scripts/verify_active_session_count.py`
Expected: `ImportError: cannot import name 'count_active_sessions' from 'statusline_lib'`

- [x] **Step 3: Implement the helper in `statusline_lib.py`**

First, add `import time` to the stdlib imports at the top of the file (the current imports are `glob`, `json`, `os`, `shutil`, `subprocess`; insert `time` in alphabetical position). Then append the helper after the existing module-level helpers — a good landing spot is just before the `# --- Pricing` section, since the helper is independent of cost handling and groups naturally with other small utilities:

```python
# --- Multi-session warning -------------------------------------------------
# Detects other Claude Code sessions that have recently touched a JSONL in
# the same project slug dir, so the statusline can warn the user at session
# start that a second instance is running here.

_SESSION_WINDOW_SECONDS = 300


def count_active_sessions(transcript_path, now=None, window_seconds=_SESSION_WINDOW_SECONDS):
    """Return how many top-level *.jsonl files in the same project slug dir
    have an mtime within the last `window_seconds`. The current session's
    JSONL is included (its mtime gets bumped on every statusline render).

    Returns 0 on any error (missing dir, permission denied, empty path).
    Subagent JSONLs live in subdirectories and are not counted.
    """
    if not transcript_path:
        return 0
    slug_dir = os.path.dirname(transcript_path)
    if not slug_dir:
        return 0
    cutoff = (now if now is not None else time.time()) - window_seconds
    try:
        count = 0
        with os.scandir(slug_dir) as it:
            for entry in it:
                if not entry.name.endswith(".jsonl"):
                    continue
                if not entry.is_file():
                    continue
                try:
                    if entry.stat().st_mtime >= cutoff:
                        count += 1
                except OSError:
                    continue
        return count
    except OSError:
        return 0
```

- [x] **Step 4: Run script to verify it passes**

Run: `python C:/Users/mtsch/schoen-claude-status/scripts/verify_active_session_count.py`
Expected: `OK: count_active_sessions behaves correctly across all cases`

- [x] **Step 5: Commit**

```bash
git -C C:/Users/mtsch/schoen-claude-status add statusline_lib.py scripts/verify_active_session_count.py
git -C C:/Users/mtsch/schoen-claude-status commit -m "feat: add count_active_sessions helper

Counts top-level *.jsonl files in the project slug dir whose mtime
falls within a configurable window (default 300s). Subagent JSONLs in
subdirs are not counted. Verify script covers empty dir, fresh, stale,
subagent, non-jsonl, and missing-dir cases."
```

---

### Task 2: Wire the helper into `statusline.py:main()` and render the label

**Files:**
- Modify: `statusline.py:75-132` (the `main()` function and its line1 assembly)

- [x] **Step 1: Update imports and main() to splice the label**

In `statusline.py`, add `RED`, `RESET`, and `count_active_sessions` to the existing import from `statusline_lib`:

```python
from statusline_lib import (
    RED,
    RESET,
    count_active_sessions,
    format_beacon,
    format_cache,
    format_calibrated_eta,
    format_context,
    format_cost,
    format_quota,
    walk_transcript,
)
```

In `main()`, after the existing `line1` is built but before the `if branch:` block, splice in the session label. Find this block (around lines 118-123):

```python
    # --- Assemble.
    spinner = _SPINNER_FRAMES[int(time.time() * 4) % len(_SPINNER_FRAMES)]
    line1 = f"{spinner} [{_hostname()}] {cwd}"
    branch = _git_branch(cwd)
    if branch:
        line1 = f"{line1} ({branch})"
```

Replace with:

```python
    # --- Assemble.
    spinner = _SPINNER_FRAMES[int(time.time() * 4) % len(_SPINNER_FRAMES)]
    line1 = f"{spinner} [{_hostname()}] {cwd}"
    n_sessions = count_active_sessions(d.get("transcript_path") or "")
    if n_sessions >= 2:
        line1 = f"{line1} {RED}[{n_sessions} sessions]{RESET}"
    branch = _git_branch(cwd)
    if branch:
        line1 = f"{line1} ({branch})"
```

- [x] **Step 2: Smoke-test against a synthetic payload**

The simplest smoke test is to pipe a minimal payload through `statusline.py` with a `transcript_path` pointing at a temp slug dir that has two fresh JSONLs. Run this from PowerShell:

```powershell
$slug = New-Item -ItemType Directory -Force "$env:TEMP\statusline-smoke"
$now = [DateTimeOffset]::Now.ToUnixTimeSeconds()
"{}" | Out-File -Encoding utf8 "$slug\self.jsonl"
"{}" | Out-File -Encoding utf8 "$slug\other.jsonl"
$payload = @{
    workspace = @{ current_dir = "C:/tmp" }
    transcript_path = "$slug\self.jsonl"
    session_id = "smoke"
    cost = @{ total_cost_usd = 0 }
} | ConvertTo-Json -Compress
$payload | python C:/Users/mtsch/schoen-claude-status/statusline.py
```

Expected: line 1 contains the red-coded `[2 sessions]` between the cwd and (no branch, since `C:/tmp` isn't a repo). The literal ANSI sequence `\x1b[31m[2 sessions]\x1b[0m` should appear in the output.

Now repeat with only one fresh JSONL:

```powershell
Remove-Item "$slug\other.jsonl"
$payload | python C:/Users/mtsch/schoen-claude-status/statusline.py
```

Expected: line 1 has NO session label (count is 1, below the ≥2 threshold).

- [x] **Step 3: Commit**

```bash
git -C C:/Users/mtsch/schoen-claude-status add statusline.py
git -C C:/Users/mtsch/schoen-claude-status commit -m "feat: render [N sessions] warning on statusline line 1

When two or more JSONL transcripts in the current project's slug dir
have an mtime within the last 5 minutes, splice a red [N sessions]
label between the cwd and the branch on line 1. The label disappears
naturally once other sessions go idle."
```

---

### Task 3: Document the label in README

**Files:**
- Modify: `README.md:19-23` (the "What you see" / Line 1 description)

- [x] **Step 1: Read current README line-1 description**

Open `README.md` and locate the "Line 1" sentence (around line 21):

```markdown
**Line 1** — hostname, current working directory, current git branch (if any).
```

- [x] **Step 2: Extend the Line 1 description**

Replace the single sentence with:

```markdown
**Line 1** — hostname, current working directory, an optional red
`[N sessions]` warning when two or more Claude Code sessions have
written to a transcript in this project directory within the last
5 minutes, and current git branch (if any). The session warning
disappears on its own once the other sessions go idle.
```

- [x] **Step 3: Commit**

```bash
git -C C:/Users/mtsch/schoen-claude-status add README.md
git -C C:/Users/mtsch/schoen-claude-status commit -m "docs: describe [N sessions] warning on README line 1"
```

---

## Self-review notes

- Spec coverage: detection rule (Task 1), display rule + position (Task 2), failure-mode silence (Task 1, `try/except OSError`), README mention (Task 3). All sections covered.
- No placeholders. Every code block is concrete.
- Type/name consistency: `count_active_sessions` used identically in helper definition (Task 1, Step 3), verify script (Task 1, Step 1), and statusline.py import + call (Task 2, Step 1). Constants `RED`, `RESET` already exist in `statusline_lib.py` and are imported by name.
- `import time` is explicitly added to `statusline_lib.py` in Task 1 Step 3 (it is not currently imported there). The helper uses `time.time()` directly.
