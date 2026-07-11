"""Verify statusline_lib.agy.format_agy_cache: a reduced, per-turn cache field
built from Antigravity's `context_window.current_usage` payload block, since
agy's brain transcripts carry no usage data for the session-cumulative walk
the Claude/Qwen cache column uses.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.agy import format_agy_cache
from statusline_lib.base import CACHE_READ, CACHE_WRITE, RESET


def _check_empty_or_malformed(failures):
    if format_agy_cache(None) != "":
        failures.append("format_agy_cache: None should render ''")
    if format_agy_cache({}) != "":
        failures.append("format_agy_cache: empty dict should render ''")
    if format_agy_cache("nope") != "":
        failures.append("format_agy_cache: non-dict should render ''")


def _check_renders_read_write_hit(failures):
    usage = {
        "input_tokens": 4917,
        "output_tokens": 254,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 147145,
    }
    out = format_agy_cache(usage)
    if CACHE_READ not in out:
        failures.append(
            f"format_agy_cache: should carry the read identity color, got {out!r}"
        )
    if CACHE_WRITE not in out:
        failures.append(
            f"format_agy_cache: should carry the write identity color, got {out!r}"
        )
    if "hit" not in out:
        failures.append(f"format_agy_cache: should render a hit% figure, got {out!r}")
    if "$" in out:
        failures.append(
            f"format_agy_cache: agy has no cost data, must never show a $ figure, got {out!r}"
        )
    if RESET not in out:
        failures.append("format_agy_cache: colored output must reset")


def _check_no_cache_activity_is_empty(failures):
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    if format_agy_cache(usage) != "":
        failures.append(
            "format_agy_cache: zero cache read/write/input should render ''"
        )


def _check_show_hit_toggle(failures):
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1000,
    }
    on = format_agy_cache(usage, show_hit=True)
    off = format_agy_cache(usage, show_hit=False)
    if "hit" not in on:
        failures.append(
            f"format_agy_cache: show_hit=True should include hit%, got {on!r}"
        )
    if "hit" in off:
        failures.append(
            f"format_agy_cache: show_hit=False should drop hit%, got {off!r}"
        )


def _check_missing_keys_default_to_zero(failures):
    # A payload that carries only some of the four keys must not crash.
    out = format_agy_cache({"cache_read_input_tokens": 500})
    if CACHE_READ not in out:
        failures.append(
            f"format_agy_cache: partial payload should still render read data, got {out!r}"
        )


def check(failures):
    _check_empty_or_malformed(failures)
    _check_renders_read_write_hit(failures)
    _check_no_cache_activity_is_empty(failures)
    _check_show_hit_toggle(failures)
    _check_missing_keys_default_to_zero(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: format_agy_cache renders a per-turn, cost-free cache field from current_usage"
    )


if __name__ == "__main__":
    main()
