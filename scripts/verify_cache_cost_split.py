"""Verify cost-component accumulation: read_cost / write_cost (model-accurate,
parent + subagents) and parent-only TTL eviction count + wasted-$.

Builds real temp JSONL transcripts and runs walk_transcript over them, so the
init -> accumulate -> return path is exercised end to end.
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
    if write:
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


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _check_components_and_evictions(failures):
    # Opus 5.0/Mtok: m1 write5000(1st,skip) m2 read20000+write2000(read>0,skip) m3 read0+write30000(evict) m4 write500(<floor,skip)
    # m2 wrote a 1h cache, so m3 must sit >3600s after it to read as a real TTL expiry.
    lines = [
        _turn("m1", read=0, write=5000, ts="2026-06-02T15:00:00.000Z"),
        _turn("m2", read=20000, write=2000, ts="2026-06-02T15:00:10.000Z"),
        _turn("m3", read=0, write=30000, ts="2026-06-02T16:30:00.000Z"),
        _turn("m4", read=0, write=500, ts="2026-06-02T16:30:01.000Z"),
    ]
    tmp = tempfile.mkdtemp(prefix="cost-split-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    exp_read_cost = 20000 * 5.0 * 0.1 / 1e6
    exp_write_cost = (5000 + 2000 + 30000 + 500) * 5.0 * 1.25 / 1e6
    exp_wasted = 30000 * 5.0 * 1.15 / 1e6

    if not _approx(walk["read_cost"], exp_read_cost):
        failures.append(f"read_cost {walk['read_cost']!r} != {exp_read_cost!r}")
    if not _approx(walk["write_cost"], exp_write_cost):
        failures.append(f"write_cost {walk['write_cost']!r} != {exp_write_cost!r}")
    if walk["ttl_evictions"] != 1:
        failures.append(f"ttl_evictions {walk['ttl_evictions']!r} != 1")
    if not _approx(walk["ttl_wasted"], exp_wasted):
        failures.append(f"ttl_wasted {walk['ttl_wasted']!r} != {exp_wasted!r}")


def _check_subagent_evictions_excluded(failures):
    # A subagent's first turn is a full write by construction; it must NOT count
    # as a parent TTL eviction, but its write_cost MUST still accumulate.
    tmp = tempfile.mkdtemp(prefix="cost-split-sub-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, [_turn("p1", read=0, write=4000)])  # parent turn 1 only
    sub_dir = os.path.join(tmp, "sess", "subagents")
    os.makedirs(sub_dir)
    _write_jsonl(
        os.path.join(sub_dir, "agent-x.jsonl"),
        [_turn("a1", read=0, write=8000), _turn("a2", read=0, write=9000)],
    )

    walk = walk_transcript(parent, include_subagents=True)

    # Parent has only its (excluded) first turn -> zero evictions overall.
    if walk["ttl_evictions"] != 0:
        failures.append(
            f"subagent writes must not count as evictions; got {walk['ttl_evictions']}"
        )
    # write_cost spans parent + both subagent turns.
    exp_write_cost = (4000 + 8000 + 9000) * 5.0 * 1.25 / 1e6
    if not _approx(walk["write_cost"], exp_write_cost):
        failures.append(
            f"write_cost should include subagents: {walk['write_cost']!r} != {exp_write_cost!r}"
        )


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
    tmp = tempfile.mkdtemp(prefix="cost-split-gap-")
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
        tmp = tempfile.mkdtemp(prefix=f"cost-split-ttl-{ttl}-")
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
    tmp = tempfile.mkdtemp(prefix="cost-split-nots-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["ttl_evictions"] != 0:
        failures.append(
            f"unknown gap (no timestamps) must not count; got {walk['ttl_evictions']}"
        )


def _check_format_cache_render(failures):
    from statusline_lib.cost import format_cache

    full = format_cache(11_980_000, 428_100, 10, 1.20, 2.14)
    if "($1.20)" not in full or "($2.14)" not in full:
        failures.append(f"full cache should show both $ parens; got {full!r}")
    if "hit" not in full:
        failures.append(f"full cache should show hit%; got {full!r}")

    no_costs = format_cache(11_980_000, 428_100, 10, 1.20, 2.14, show_costs=False)
    if "$" in no_costs:
        failures.append(f"show_costs=False must drop $ parens; got {no_costs!r}")

    no_hit = format_cache(11_980_000, 428_100, 10, 1.20, 2.14, show_hit=False)
    if "hit" in no_hit:
        failures.append(f"show_hit=False must drop hit%; got {no_hit!r}")

    # Back-compat: no cost args (subagent caller) -> no parens, byte path intact.
    legacy = format_cache(11_980_000, 428_100, 10)
    if "$" in legacy or "hit" not in legacy:
        failures.append(f"legacy 3-arg call should match old output; got {legacy!r}")


def check(failures):
    _check_components_and_evictions(failures)
    _check_subagent_evictions_excluded(failures)
    _check_small_gap_not_evicted(failures)
    _check_ttl_threshold_derived_from_write(failures)
    _check_missing_timestamps_not_evicted(failures)
    _check_format_cache_render(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: cost components + parent-only TTL evictions accumulate correctly")


if __name__ == "__main__":
    main()
