"""Verify statusline_lib/render_line2.py, the line-2 formatter extracted from
statusline.py so the resident server can render without the entry script.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.compact import resolve_flags
from statusline_lib.render_line2 import Line2Inputs, hide_cost, render_line2

_WALK = {
    "read": 40000,
    "write": 100,
    "input": 10,
    "output": 50,
    "read_cost": 0.012,
    "write_cost": 0.000375,
    "input_cost": 0.00015,
    "output_cost": 0.00375,
    "ttl_evictions": 2,
    "ttl_wasted": 0.5,
}

# All-zero walk: format_cache's own guard (read + write + input_t <= 0) makes
# the session-cumulative cache field render "", the precondition for the agy
# per-turn fallback in render_line2 to fire.
_EMPTY_WALK = {
    "read": 0,
    "write": 0,
    "input": 0,
    "output": 0,
    "read_cost": 0.0,
    "write_cost": 0.0,
    "input_cost": 0.0,
    "output_cost": 0.0,
    "ttl_evictions": 0,
    "ttl_wasted": 0.0,
}


def _inputs(**overrides):
    fields = {
        "model_summary": "Opus",
        "context_used": 40110,
        "window_size": 200000,
        "model_id": "claude-opus-4-8",
        "walk": _WALK,
        "rate_limits": None,
        "day_budget_summary": "day $1.00",
        "cost_summary": "$2.50",
        "hide_cost": False,
        "lines_summary": "+10/-2",
    }
    fields.update(overrides)
    return Line2Inputs(**fields)


def check_full_line_carries_every_field(failures):
    flags = resolve_flags(lambda f: "", None)
    rendered = render_line2(flags, _inputs())
    for fragment in ("$2.50", "day $1.00", "+10/-2"):
        if fragment not in rendered:
            failures.append(f"line 2 lost {fragment!r}: {rendered!r}")


def check_hide_cost_suppresses_every_dollar_figure(failures):
    flags = resolve_flags(lambda f: "", None)
    rendered = render_line2(flags, _inputs(hide_cost=True))
    if "$" in rendered:
        failures.append(f"hide_cost must suppress every dollar figure: {rendered!r}")
    if "+10/-2" not in rendered:
        failures.append("the diffstat is not money and must survive hide_cost")


def check_hide_cost_reads_the_pref(failures):
    # Isolated from any real ~/.claude/.statusline-prefs.json: point the prefs
    # resolver at a file inside a fresh temp directory (never written, so
    # load_prefs() falls through to {}) rather than relying on a real prefs
    # file being absent on the machine running this check.
    saved_hide_cost = os.environ.get("STATUSLINE_HIDE_COST")
    saved_prefs_path = os.environ.get("STATUSLINE_PREFS_PATH")
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["STATUSLINE_PREFS_PATH"] = os.path.join(tmp_dir, "unused-prefs.json")
        os.environ["STATUSLINE_HIDE_COST"] = "1"
        try:
            if hide_cost() is not True:
                failures.append("STATUSLINE_HIDE_COST=1 must resolve to True")
        finally:
            if saved_hide_cost is None:
                os.environ.pop("STATUSLINE_HIDE_COST", None)
            else:
                os.environ["STATUSLINE_HIDE_COST"] = saved_hide_cost
            if saved_prefs_path is None:
                os.environ.pop("STATUSLINE_PREFS_PATH", None)
            else:
                os.environ["STATUSLINE_PREFS_PATH"] = saved_prefs_path


def check_agy_payload_falls_back_to_per_turn_cache(failures):
    # Antigravity payload: an empty transcript walk (agy's brain transcripts
    # carry no usage data) makes the session-cumulative cache field render "",
    # which should fall back to the per-turn current_usage snapshot.
    flags = resolve_flags(lambda f: "", None)
    inputs = _inputs(
        walk=_EMPTY_WALK,
        current_usage={
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 0,
            "input_tokens": 20,
        },
        is_agy=True,
    )
    rendered = render_line2(flags, inputs)
    if "turn" not in rendered:
        failures.append(
            f"agy payload with an empty walk should fall back to the per-turn "
            f"cache snapshot: {rendered!r}"
        )


def check_rate_limits_render_quota_and_fable_pool(failures):
    # A payload carrying rate_limits exercises the format_quota arm (instead
    # of the agy_quota fallback) and the format_fable_quota arm (instead of
    # the unconditional "").
    flags = resolve_flags(lambda f: "", None)
    rate_limits = {
        "five_hour": {"used_percentage": 12.0, "resets_at": 1_700_018_000},
        "seven_day": {"used_percentage": 30.0, "resets_at": 1_700_600_000},
    }
    rendered = render_line2(flags, _inputs(rate_limits=rate_limits))
    if "5h:" not in rendered or "wk:" not in rendered:
        failures.append(
            f"a rate_limits payload should render the 5h and wk quota windows: "
            f"{rendered!r}"
        )


def main():
    failures = []
    check_full_line_carries_every_field(failures)
    check_hide_cost_suppresses_every_dollar_figure(failures)
    check_hide_cost_reads_the_pref(failures)
    check_agy_payload_falls_back_to_per_turn_cache(failures)
    check_rate_limits_render_quota_and_fable_pool(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: render_line2 extracted; hide_cost, agy fallback, and quota arms covered")


if __name__ == "__main__":
    main()
