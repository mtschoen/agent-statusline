"""Verify statusline_lib.agy.format_agent_state: a small muted tag for
Antigravity's `agent_state` field, absent when the field is absent (as it
always is on a Claude Code payload).

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.agy import _AGENT_STATE_COLOR, format_agent_state
from statusline_lib.base import RESET


def _check_absent_field(failures):
    if format_agent_state(None) != "":
        failures.append("format_agent_state: None should render ''")
    if format_agent_state("") != "":
        failures.append("format_agent_state: '' should render ''")
    if format_agent_state("   ") != "":
        failures.append("format_agent_state: whitespace-only should render ''")


def _check_known_states_get_glyphs(failures):
    working = format_agent_state("working")
    if "●" not in working or "working" not in working:
        failures.append(
            f"format_agent_state: 'working' should show its glyph+label, got {working!r}"
        )
    idle = format_agent_state("idle")
    if "○" not in idle or "idle" not in idle:
        failures.append(
            f"format_agent_state: 'idle' should show its glyph+label, got {idle!r}"
        )
    if _AGENT_STATE_COLOR not in working or _AGENT_STATE_COLOR not in idle:
        failures.append(
            "format_agent_state: known states should use the muted tag color"
        )
    if RESET not in working:
        failures.append("format_agent_state: colored output must reset")


def _check_case_insensitive_glyph_lookup(failures):
    if "●" not in format_agent_state("Working"):
        failures.append("format_agent_state: glyph lookup should be case-insensitive")


def _check_unknown_state_still_renders(failures):
    out = format_agent_state("blocked")
    if "blocked" not in out:
        failures.append(
            f"format_agent_state: unrecognized state should still show the raw label, got {out!r}"
        )
    if "●" in out or "○" in out:
        failures.append(
            f"format_agent_state: unrecognized state should have no glyph, got {out!r}"
        )


def _check_non_string_input_coerces(failures):
    # A harness sending a non-string agent_state must not crash the .strip() call.
    out = format_agent_state(42)
    if "42" not in out:
        failures.append(
            f"format_agent_state: non-string input should coerce to str, got {out!r}"
        )


def check(failures):
    _check_absent_field(failures)
    _check_known_states_get_glyphs(failures)
    _check_case_insensitive_glyph_lookup(failures)
    _check_unknown_state_still_renders(failures)
    _check_non_string_input_coerces(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: format_agent_state renders agy's agent_state as a muted tag")


if __name__ == "__main__":
    main()
