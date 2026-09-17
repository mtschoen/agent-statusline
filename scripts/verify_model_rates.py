"""Verify per-model $/MTok rate selection and cache-read/write multipliers.

Split out of verify_cache_cost_split.py once this suite's own growth (the
Sonnet 5 pricing-cancellation fix + the Fable 5.1 cache-read fix) pushed that
file past aislop's 400-line reviewability gate. Covers `_rates_for` (base
input/output $/MTok per model id), `_cache_read_mult` (the per-model
cache-read multiplier), and `_write_cost` (the 5m/1h cache-write split) --
the three lookups `_cost_for_turn` and `_accumulate_assistant_turn` both
depend on for a session's dollar total.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _check_model_rates(failures):
    # Fable 5 bills $10/$50 per MTok (platform.claude.com models overview).
    # Before the fable row existed, _rates_for fell back to sonnet ($3/$15)
    # and every fable session under-estimated cost 3.33x. Mythos ids map to
    # the same Fable family.
    #
    # Sonnet 5's $2/$10 launch pricing was originally introductory, with a
    # scheduled increase to $3/$15 on 2026-09-01. That increase was announced
    # and then CANCELLED: $2/$10 is Sonnet 5's permanent, unconditional price
    # (platform.claude.com/docs/en/about-claude/pricing, fetched 2026-09-17).
    # This is the regression guard for the cancelled increase ever having
    # shipped live -- claude-sonnet-5 must return (2.0, 10.0) unconditionally,
    # with no date dependence at all. "sonnet-5" must NOT catch "sonnet-4-5"
    # or "sonnet-4-6".
    from statusline_lib.cost import _rates_for

    cases = [
        ("claude-fable-5", (10.0, 50.0)),
        ("claude-fable-5[1m]", (10.0, 50.0)),
        ("claude-mythos-preview", (10.0, 50.0)),
        ("claude-opus-4-8", (5.0, 25.0)),
        ("claude-haiku-4-5", (1.0, 5.0)),
        ("totally-unknown-model", (3.0, 15.0)),  # sonnet fallback
        # Sonnet 5: always $2/$10, unconditionally.
        ("claude-sonnet-5", (2.0, 10.0)),
        # Non-collision: sonnet-4-5/4-6 stay generic sonnet.
        ("claude-sonnet-4-5", (3.0, 15.0)),
        ("claude-sonnet-4-6", (3.0, 15.0)),
    ]
    for model_id, expected in cases:
        got = _rates_for(model_id)
        if got != expected:
            failures.append(f"rates for {model_id!r}: {got!r} != {expected!r}")


def _check_cache_read_multiplier(failures):
    # Cache reads bill at 0.1x base input, except 0.025x on Claude Fable 5.1
    # and Claude Mythos 5.1 specifically (platform.claude.com/docs/en/
    # about-claude/pricing). This is model-id-level, not family-level:
    # _RATES["fable"] covers fable-5, mythos-5, fable-5-1, and mythos-5-1
    # alike (same $10/$50 base), but only the 5.1 pair gets the cheaper read
    # rate. "fable-5" is a substring of "fable-5-1", so an unordered check
    # would let 5.1 ids fall through to 0.1x (a 4x overcharge) -- this pins
    # the ordering.
    from statusline_lib.cost import _cache_read_mult

    cases = [
        ("claude-fable-5-1", 0.025),
        ("claude-mythos-5-1", 0.025),
        ("claude-fable-5", 0.1),
        ("claude-mythos-5", 0.1),
        ("claude-opus-5", 0.1),
        ("claude-sonnet-5", 0.1),
        ("claude-haiku-4-5", 0.1),
    ]
    for model_id, expected in cases:
        got = _cache_read_mult(model_id)
        if got != expected:
            failures.append(
                f"cache-read multiplier for {model_id!r}: {got!r} != {expected!r}"
            )


def _check_fable_5_1_cache_read_end_to_end(failures):
    # End-to-end proof the 4x overcharge is actually fixed at the
    # walk_transcript level, not just in the multiplier lookup: a Fable 5.1
    # turn with a large cache read must bill that read at 0.025x, not 0.1x.
    from statusline_lib.cost import _cost_for_turn

    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 100_000,
        "output_tokens": 500,
    }
    got = _cost_for_turn(usage, "claude-fable-5-1")
    # fable base rates: 10.0 in / 50.0 out per MTok.
    expected = (1000 * 10.0 + 100_000 * (10.0 * 0.025) + 500 * 50.0) / 1_000_000.0
    if not _approx(got, expected):
        failures.append(
            f"fable-5.1 turn cost {got!r} != {expected!r} "
            f"(cache read must bill at 0.025x, not 0.1x)"
        )
    # And the old (wrong) 0.1x multiplier would have been 4x this read's
    # contribution -- assert the two are actually distinguishable.
    wrong = (1000 * 10.0 + 100_000 * (10.0 * 0.1) + 500 * 50.0) / 1_000_000.0
    if _approx(got, wrong):
        failures.append(
            "fable-5.1 turn cost matches the old 0.1x multiplier -- "
            "the 4x cache-read overcharge is not actually fixed"
        )


def _check_write_cost_multipliers(failures):
    # Lock in the 5m/1h cache-write split (1.25x / 2.0x of base input) via
    # _write_cost directly, independent of the walk-level coverage in
    # verify_cache_cost_split.py.
    from statusline_lib.cost import WRITE_MULT_1H, WRITE_MULT_5M, _write_cost

    if WRITE_MULT_5M != 1.25:
        failures.append(f"WRITE_MULT_5M {WRITE_MULT_5M!r} != 1.25")
    if WRITE_MULT_1H != 2.0:
        failures.append(f"WRITE_MULT_1H {WRITE_MULT_1H!r} != 2.0")

    inp_rate = 10.0
    five_min_usage = {"cache_creation": {"ephemeral_5m_input_tokens": 40_000}}
    got_5m = _write_cost(five_min_usage, inp_rate)
    exp_5m = 40_000 * 1.25 * inp_rate / 1_000_000.0
    if not _approx(got_5m, exp_5m):
        failures.append(f"5m write_cost {got_5m!r} != {exp_5m!r}")

    hour_usage = {"cache_creation": {"ephemeral_1h_input_tokens": 40_000}}
    got_1h = _write_cost(hour_usage, inp_rate)
    exp_1h = 40_000 * 2.0 * inp_rate / 1_000_000.0
    if not _approx(got_1h, exp_1h):
        failures.append(f"1h write_cost {got_1h!r} != {exp_1h!r}")


def check(failures):
    _check_model_rates(failures)
    _check_cache_read_multiplier(failures)
    _check_fable_5_1_cache_read_end_to_end(failures)
    _check_write_cost_multipliers(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: model rates + cache read/write multipliers are correct")


if __name__ == "__main__":
    main()
