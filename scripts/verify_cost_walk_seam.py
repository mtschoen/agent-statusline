"""Verify the cost.py transcript-walk accumulator seam: new_walk_accumulator,
fold_transcript_lines, and summarize_walk must compose to reproduce
walk_transcript's one-call output exactly, folding one line at a time. This
is the contract the resident server's incremental walk depends on, since it
only folds the transcript bytes appended since the last render rather than
rewalking the whole file.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.cost import (
    fold_transcript_lines,
    new_walk_accumulator,
    summarize_walk,
    walk_transcript,
)


def _turn(
    mid, read, write, inp=10, out=100, model="claude-opus-4-8", ts=None, ttl="1h"
):
    usage = {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
        "output_tokens": out,
    }
    if write and ttl is not None:
        # Mirror the real transcript: a write carries the TTL bucket it used.
        key = f"ephemeral_{'1h' if ttl == '1h' else '5m'}_input_tokens"
        usage["cache_creation"] = {key: write}
    entry = {
        "type": "assistant",
        "message": {"role": "assistant", "id": mid, "model": model, "usage": usage},
    }
    if ts is not None:
        entry["timestamp"] = ts
    return json.dumps(entry)


def _fixture_lines():
    """A handful of assistant turns with distinct message ids and usage
    blocks, plus one typed user prompt, so both eviction tracking and
    prompt counting are exercised by check_the_seam_reproduces_walk_transcript.
    """
    return [
        _turn("f1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("f2", read=20000, write=2000, ts="2026-06-02T15:00:10.000Z"),
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": "hello there"}}
        ),
        _turn("f3", read=0, write=30000, ts="2026-06-02T16:30:00.000Z"),
    ]


def check_the_seam_reproduces_walk_transcript(failures):
    """Folding a transcript one line at a time through the seam must produce
    exactly what walk_transcript produces in one call. This is the contract
    the server's incremental walk depends on."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        lines = _fixture_lines()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        expected = walk_transcript(path)

        accumulator = new_walk_accumulator()
        accumulator["track_evictions"] = True
        accumulator["track_user_prompts"] = True
        seen_ids = set()
        for line in lines:
            fold_transcript_lines([line], accumulator, seen_ids)
        actual = summarize_walk(accumulator, accumulator["cost"])

        if actual != expected:
            failures.append(
                "line-at-a-time folding diverged from walk_transcript:"
                f" {actual} != {expected}"
            )


def check_new_walk_accumulator_defaults_tracking_off(failures):
    accumulator = new_walk_accumulator()
    if accumulator["track_evictions"] or accumulator["track_user_prompts"]:
        failures.append(
            "a fresh accumulator must not track evictions or user prompts:"
            " those are parent-only and the caller opts in"
        )


def check(failures):
    check_the_seam_reproduces_walk_transcript(failures)
    check_new_walk_accumulator_defaults_tracking_off(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: cost_walk accumulator seam reproduces walk_transcript's output")


if __name__ == "__main__":
    main()
