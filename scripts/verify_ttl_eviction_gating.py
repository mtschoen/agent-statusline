"""Verify the TTL eviction counter's gating logic: a substantial rewrite
(w >= TTL_MIN_WRITE_TOKENS) after an idle gap that exceeds the prior turn's
written TTL counts as an eviction - with NO read-based condition at all.

Split out of verify_cache_cost_split.py (which also covers this ground for
the base accumulation/format_cache checks) once the partial-hit test additions
pushed that file over aislop's 400-line file gate - same reason cost.py and
costfmt.py are two files instead of one. Builds real temp JSONL transcripts
and runs walk_transcript over them end to end, same fixture style as the file
it was split from.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.cost import walk_transcript


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
        key = f"ephemeral_{'1h' if ttl == '1h' else '5m'}_input_tokens"
        usage["cache_creation"] = {key: write}
    entry = {
        "type": "assistant",
        "message": {"role": "assistant", "id": mid, "model": model, "usage": usage},
    }
    if ts is not None:
        entry["timestamp"] = ts
    return json.dumps(entry)


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _check_small_gap_not_evicted(failures):
    # A read==0 / write>=floor turn that lands only seconds after the previous
    # turn is a tool-array/compaction cache bust, NOT an idle TTL expiry (the
    # 5-min cache clock never lapsed). The idle-gap gate must suppress it.
    # Mirrors file-wizard turn #16: ToolSearch loaded a deferred tool, busting
    # the prefix cache 3s later.
    lines = [
        _turn("g1", read=100000, write=4000, ts="2026-06-02T15:08:20.000Z"),
        _turn("g2", read=0, write=117747, ts="2026-06-02T15:08:23.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-gap-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"sub-300s gap must not count as a TTL eviction; got {walk['ttl_evictions']}"
        )
    if walk["ttl_wasted"] != 0.0:
        failures.append(
            f"suppressed eviction must waste $0; got {walk['ttl_wasted']!r}"
        )


def _check_ttl_threshold_derived_from_write(failures):
    # Same ~6-min idle gap, opposite verdicts depending on the prior turn's TTL:
    # a 5m-written cache has expired (counts); a 1h-written cache is still warm,
    # so the rewrite is some other bust, not a timeout (does not count).
    for ttl, expected in (("5m", 1), ("1h", 0)):
        lines = [
            _turn("a1", read=50000, write=4000, ts="2026-06-02T15:00:00.000Z", ttl=ttl),
            _turn("a2", read=0, write=30000, ts="2026-06-02T15:06:00.000Z", ttl=ttl),
        ]
        tmp = tempfile.mkdtemp(prefix=f"ttl-gate-{ttl}-")
        parent = os.path.join(tmp, "sess.jsonl")
        _write_jsonl(parent, lines)

        walk = walk_transcript(parent, include_subagents=True)

        if walk["ttl_evictions"] != expected:
            failures.append(
                f"{ttl} cache, 6-min gap: expected {expected} eviction(s); "
                f"got {walk['ttl_evictions']}"
            )


def _check_missing_timestamps_not_evicted(failures):
    # Without timestamps the idle gap is unknowable, so a TTL eviction cannot be
    # asserted - the gate stays conservative and counts nothing.
    lines = [
        _turn("n1", read=0, write=5000),
        _turn("n2", read=0, write=30000),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-nots-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"unknown gap (no timestamps) must not count; got {walk['ttl_evictions']}"
        )


def _check_partial_hit_evicted(failures):
    # Real-world shape (2026-07-11, four sessions resumed after ~8.6h idle): only
    # the FIRST resume gets read==0; the other three read the shared system-prompt
    # prefix the first resume just re-warmed (r=24299, identical across three
    # projects) while still rewriting the whole conversation (w=202628). A strict
    # r==0 gate misses these real evictions entirely - the gate now ignores the
    # read count altogether, so this counts on the write + idle-gap facts alone.
    lines = [
        _turn("p1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("p2", read=24299, write=202628, ts="2026-06-02T16:30:00.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-partial-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 1:
        failures.append(
            f"partial-hit shared-prefix rewrite must count as an eviction; "
            f"got {walk['ttl_evictions']}"
        )
    exp_wasted = 202628 * 5.0 * 1.15 / 1e6
    if not _approx(walk["ttl_wasted"], exp_wasted):
        failures.append(f"ttl_wasted {walk['ttl_wasted']!r} != {exp_wasted!r}")


def _check_small_session_partial_hit_evicted(failures):
    # The case a read:write RATIO gate would have missed: the same fixed
    # ~24k shared-prefix read as the real-world shape above, but a modest
    # rewrite (w=10000) that a read-dominance ratio would read as "mostly a
    # cache hit" and wrongly exclude. The gate has no read condition at all,
    # so a small session's real eviction still counts on write + idle-gap
    # alone.
    lines = [
        _turn("s1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("s2", read=24299, write=10000, ts="2026-06-02T16:30:00.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-small-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 1:
        failures.append(
            f"small-session rewrite dwarfed by the shared-prefix read must "
            f"still count as an eviction; got {walk['ttl_evictions']}"
        )


def _check_warm_double_resume_below_floor_not_evicted(failures):
    # A warm double-resume: mostly cache hit (large read), small incidental
    # write, well past the idle-gap threshold. With no read condition, only
    # the write floor keeps this quiet - w=800 sits below TTL_MIN_WRITE_TOKENS.
    lines = [
        _turn("d1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("d2", read=180000, write=800, ts="2026-06-02T16:30:00.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-warmdouble-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"warm double-resume write below TTL_MIN_WRITE_TOKENS must not "
            f"count; got {walk['ttl_evictions']}"
        )


def _check_partial_hit_gap_not_exceeded_not_evicted(failures):
    # Same partial-hit read/write shape, but the idle gap since the prior turn
    # does NOT exceed the prior turn's written TTL - still not a TTL expiry.
    lines = [
        _turn("g1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("g2", read=24299, write=202628, ts="2026-06-02T15:00:10.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-partial-gap-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"partial-hit shape without an exceeded idle gap must not count; "
            f"got {walk['ttl_evictions']}"
        )


def _check_first_turn_not_evicted(failures):
    # The very first parent turn is always a full write by construction (there
    # is nothing to have evicted yet) - must never count even with a huge
    # write and a timestamp present.
    lines = [
        _turn("first", read=0, write=500000, ts="2026-06-02T15:00:00.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="ttl-gate-first-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"the first parent turn must never count as an eviction; "
            f"got {walk['ttl_evictions']}"
        )


def check(failures):
    _check_small_gap_not_evicted(failures)
    _check_ttl_threshold_derived_from_write(failures)
    _check_missing_timestamps_not_evicted(failures)
    _check_partial_hit_evicted(failures)
    _check_small_session_partial_hit_evicted(failures)
    _check_warm_double_resume_below_floor_not_evicted(failures)
    _check_partial_hit_gap_not_exceeded_not_evicted(failures)
    _check_first_turn_not_evicted(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: TTL eviction gating (write floor + idle-gap-vs-TTL, no read condition) is correct"
    )


if __name__ == "__main__":
    main()
