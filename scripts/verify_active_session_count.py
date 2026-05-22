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

    # Case 7: default `now=` exercises the live time.time() branch.
    if count_active_sessions("") != 0:
        failures.append("default now should still return 0 for empty path")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: count_active_sessions behaves correctly across all cases")


if __name__ == "__main__":
    main()
