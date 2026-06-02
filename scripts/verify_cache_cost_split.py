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


def _turn(mid, read, write, inp=10, out=100, model="claude-opus-4-8"):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "id": mid,
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": write,
                    "output_tokens": out,
                },
            },
        }
    )


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _check_components_and_evictions(failures):
    # Opus 5.0/Mtok: m1 write5000(1st,skip) m2 read20000+write2000(read>0,skip) m3 read0+write30000(evict) m4 write500(<floor,skip)
    lines = [
        _turn("m1", read=0, write=5000),
        _turn("m2", read=20000, write=2000),
        _turn("m3", read=0, write=30000),
        _turn("m4", read=0, write=500),
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
