"""Verify cost-component accumulation: read_cost / write_cost (model-accurate,
parent + subagents) and parent-only TTL eviction count + wasted-$.

Builds real temp JSONL transcripts and runs walk_transcript over them, so the
init -> accumulate -> return path is exercised end to end. Also covers the
walk's resilience paths (bad-JSON lines, non-assistant entries, snapshot
duplicates, unreadable files, malformed timestamps) and the format_cache
empty-session guard.
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
        # Mirror the real transcript: a write carries the TTL bucket it used.
        # ttl=None omits the breakdown (older/API-key transcript) so the
        # _write_cost fallback path is exercised.
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
    # All four turns default to a 1h cache write -> 2.0x base input (not 1.25x).
    exp_write_cost = (5000 + 2000 + 30000 + 500) * 5.0 * 2.0 / 1e6
    exp_wasted = 30000 * 5.0 * 1.15 / 1e6

    if not _approx(walk["read_cost"], exp_read_cost):
        failures.append(f"read_cost {walk['read_cost']!r} != {exp_read_cost!r}")
    if not _approx(walk["write_cost"], exp_write_cost):
        failures.append(f"write_cost {walk['write_cost']!r} != {exp_write_cost!r}")
    if walk["ttl_evictions"] != 1:
        failures.append(f"ttl_evictions {walk['ttl_evictions']!r} != 1")
    if not _approx(walk["ttl_wasted"], exp_wasted):
        failures.append(f"ttl_wasted {walk['ttl_wasted']!r} != {exp_wasted!r}")

    # The other two breakdown components: fresh input at the plain input rate
    # (5.0/Mtok opus), output at the output rate (25.0/Mtok). Four turns at the
    # _turn defaults (inp=10, out=100) -> 40 input tok, 400 output tok.
    exp_input_cost = 40 * 5.0 / 1e6
    exp_output_cost = 400 * 25.0 / 1e6
    if not _approx(walk["input_cost"], exp_input_cost):
        failures.append(f"input_cost {walk['input_cost']!r} != {exp_input_cost!r}")
    if not _approx(walk["output_cost"], exp_output_cost):
        failures.append(f"output_cost {walk['output_cost']!r} != {exp_output_cost!r}")
    # The four components must reconcile to the walk's own token cost (cost has no
    # web-search add-on in this fixture, so the sum is exact).
    exp_components = (
        walk["read_cost"]
        + walk["write_cost"]
        + walk["input_cost"]
        + walk["output_cost"]
    )
    if not _approx(walk["cost"], exp_components):
        failures.append(
            f"four components {exp_components!r} must sum to cost {walk['cost']!r}"
        )


def _check_write_cost_ttl_split(failures):
    # The core fix: a write's cost depends on its TTL. 5-minute writes bill at
    # 1.25x base input, 1-hour writes at 2.0x; a write with no ephemeral_{5m,1h}
    # breakdown (older/API-key transcript) falls back to the 1.25x path. Each
    # case is a single-turn session so write_cost isolates the multiplier.
    cases = [
        ("5m", "5m", 1.25),
        ("1h", "1h", 2.0),
        ("legacy", None, 1.25),  # no breakdown -> fallback to 1.25x
    ]
    for label, ttl, mult in cases:
        tmp = tempfile.mkdtemp(prefix=f"cost-wsplit-{label}-")
        parent = os.path.join(tmp, "sess.jsonl")
        _write_jsonl(parent, [_turn("w1", read=0, write=40000, ttl=ttl)])
        walk = walk_transcript(parent, include_subagents=False)
        exp = 40000 * 5.0 * mult / 1e6
        if not _approx(walk["write_cost"], exp):
            failures.append(
                f"{label} write_cost {walk['write_cost']!r} != {exp!r} (mult {mult})"
            )
        # write_cost must equal the write portion of the turn's total cost too,
        # so the rendered breakdown stays consistent with the cost figure.
        if not _approx(
            walk["cost"],
            walk["write_cost"]
            + walk["input_cost"]
            + walk["read_cost"]
            + walk["output_cost"],
        ):
            failures.append(f"{label}: components must reconcile to cost")


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
    # write_cost spans parent + both subagent turns (all 1h -> 2.0x).
    exp_write_cost = (4000 + 8000 + 9000) * 5.0 * 2.0 / 1e6
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
    from statusline_lib.costfmt import format_cache

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

    # Full four-way breakdown: input figure precedes read, output follows write,
    # all four $ figures present, essentials (read/write/hit%) still there.
    breakdown = format_cache(
        11_980_000,
        428_100,
        50_000,
        1.20,
        2.14,
        output_t=2_000,
        input_cost=0.25,
        output_cost=0.05,
        show_input=True,
        show_output=True,
    )
    for needle in ("($0.25)", "($1.20)", "($2.14)", "($0.05)", "hit"):
        if needle not in breakdown:
            failures.append(f"breakdown missing {needle!r}; got {breakdown!r}")
    if breakdown.index("($0.25)") >= breakdown.index("($1.20)"):
        failures.append(f"input figure must precede read; got {breakdown!r}")
    if breakdown.index("($0.05)") <= breakdown.index("($2.14)"):
        failures.append(f"output figure must follow write; got {breakdown!r}")

    # The full-breakdown figures default off, so today's callers are unaffected;
    # and each is gated on its own cost arg even when its show flag is on.
    default_off = format_cache(11_980_000, 428_100, 50_000, 1.20, 2.14)
    if "($0.25)" in default_off or "($0.05)" in default_off:
        failures.append(f"input/output must be off by default; got {default_off!r}")
    no_cost_arg = format_cache(
        11_980_000, 428_100, 50_000, 1.20, 2.14, show_input=True, show_output=True
    )
    if no_cost_arg != default_off:
        failures.append(
            f"show flags without cost args must render nothing extra; got {no_cost_arg!r}"
        )


def _check_format_cache_read_cost_not_passed_through_fmt(failures):
    # Regression guard for a real production crash (10+ occurrences in
    # ~/.claude/.statusline-error.log): format_cache must render the dollar
    # cost through its own f-string, never by handing the pre-formatted
    # `$N.NN` STRING to format_cache_read/format_cache_write, whose fmt()
    # expects a numeric token count and raises TypeError on a string. Use a
    # read count >1M so fmt()'s `n >= 1_000_000` branch actually runs -- a
    # regression here crashes regardless of which branch, but this exercises
    # the exact shape from the traceback (large read count + cost args, full
    # money/show_output breakdown enabled).
    from statusline_lib.costfmt import format_cache

    result = format_cache(
        12_345_678,
        428_100,
        50_000,
        read_cost=1.23,
        write_cost=4.56,
        output_t=2_000,
        output_cost=0.07,
        show_output=True,
    )
    if "($1.23)" not in result or "($4.56)" not in result or "($0.07)" not in result:
        failures.append(
            f"format_cache with a >1M read count and cost args must render all "
            f"three $ figures without crashing; got {result!r}"
        )


def _check_model_rates(failures):
    # Fable 5 bills $10/$50 per MTok (platform.claude.com models overview).
    # Before the fable row existed, _rates_for fell back to sonnet ($3/$15)
    # and every fable session under-estimated cost 3.33x.
    from statusline_lib.cost import _rates_for

    cases = [
        ("claude-fable-5", (10.0, 50.0)),
        ("claude-fable-5[1m]", (10.0, 50.0)),
        ("claude-opus-4-8", (5.0, 25.0)),
        ("claude-haiku-4-5", (1.0, 5.0)),
        ("totally-unknown-model", (3.0, 15.0)),  # sonnet fallback
    ]
    for model_id, expected in cases:
        got = _rates_for(model_id)
        if got != expected:
            failures.append(f"rates for {model_id!r}: {got!r} != {expected!r}")


def _check_parse_ts_bad_value(failures):
    # cost.py lines 54-55: _parse_ts returns None on a malformed ISO-8601 string.
    from statusline_lib.cost import _parse_ts

    cases = [
        (None, "None input"),
        (12345, "non-string input"),
        ("not-a-date", "garbage string"),
        ("", "empty string"),
    ]
    for value, label in cases:
        result = _parse_ts(value)
        if result is not None:
            failures.append(
                f"_parse_ts({value!r}) ({label}) should return None; got {result!r}"
            )


def _check_walk_skips_junk_lines(failures):
    # End to end through walk_transcript over a transcript that mixes a bad-JSON
    # line (cost.py 172-173: skipped silently), a non-assistant entry (line 109:
    # skipped), and a snapshot-duplicated assistant turn (line 115: the repeated
    # message.id counts once). Only the d1 turn's tokens may land in the totals.
    lines = [
        "{ not valid json",
        '{"message": {"role": "user", "id": "u1", "content": "hello"}}',
        _turn("d1", read=0, write=0, inp=10, out=100),
        _turn("d1", read=0, write=0, inp=10, out=100),  # snapshot duplicate
    ]
    tmp = tempfile.mkdtemp(prefix="cost-junk-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, lines)

    walk = walk_transcript(parent, include_subagents=True)

    if walk["input"] != 10 or walk["output"] != 100:
        failures.append(
            f"junk/duplicate lines must count d1 once: input={walk['input']}, "
            f"output={walk['output']} (want 10, 100)"
        )


def _check_walk_one_transcript_oserror(failures):
    # cost.py lines 175-178: OSError on file open is swallowed; acc is never
    # touched, so an empty dict suffices (any field access would raise KeyError
    # and fail this check).
    from statusline_lib.cost import _walk_one_transcript

    _walk_one_transcript("/nonexistent/dir/session.jsonl", {}, set())


def _check_format_cache_zero_tokens(failures):
    # costfmt.py line 63: format_cache returns "" when total_in (read+write+input) <= 0.
    from statusline_lib.costfmt import format_cache

    result = format_cache(0, 0, 0)
    if result != "":
        failures.append(f"format_cache(0, 0, 0) should return ''; got {result!r}")


def check(failures):
    _check_model_rates(failures)
    _check_components_and_evictions(failures)
    _check_write_cost_ttl_split(failures)
    _check_subagent_evictions_excluded(failures)
    _check_small_gap_not_evicted(failures)
    _check_ttl_threshold_derived_from_write(failures)
    _check_missing_timestamps_not_evicted(failures)
    _check_format_cache_render(failures)
    _check_format_cache_read_cost_not_passed_through_fmt(failures)
    _check_parse_ts_bad_value(failures)
    _check_walk_skips_junk_lines(failures)
    _check_walk_one_transcript_oserror(failures)
    _check_format_cache_zero_tokens(failures)


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
