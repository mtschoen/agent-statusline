"""Verify the TTL segment renderer: hidden at zero, loud red with the
text-presentation warning glyph otherwise, wasted-$ toggle."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.base import RED
from statusline_lib.cost import TTL_WARN_GLYPH, format_ttl


def _check_hidden_at_zero(failures):
    if format_ttl(0, 0.0) != "":
        failures.append("zero evictions must render nothing")
    if format_ttl(0, 5.0) != "":
        failures.append("zero evictions must render nothing even with wasted>0")


def _check_loud_red_with_glyph(failures):
    out = format_ttl(3, 0.85)
    if RED not in out:
        failures.append(f"TTL segment must be red; got {out!r}")
    if "TTL:3" not in out:
        failures.append(f"TTL segment must show the count; got {out!r}")
    if "(~$0.85)" not in out:
        failures.append(f"TTL segment must show wasted $; got {out!r}")
    if "︎" not in TTL_WARN_GLYPH:
        failures.append("warning glyph should carry the U+FE0E text selector")


def _check_wasted_toggle(failures):
    out = format_ttl(2, 1.50, show_wasted=False)
    if "TTL:2" not in out:
        failures.append(f"count must show with wasted off; got {out!r}")
    if "$" in out:
        failures.append(f"wasted $ must be omitted when show_wasted=False; got {out!r}")


def check(failures):
    _check_hidden_at_zero(failures)
    _check_loud_red_with_glyph(failures)
    _check_wasted_toggle(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: TTL segment renders count + wasted with the right loud styling")


if __name__ == "__main__":
    main()
