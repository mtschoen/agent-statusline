"""Verify the cost field's subagent split, web-search charge, and drift marker.

Three pieces in statusline_lib work together so the main statusline can show an
accurate cost without lying about what's measured vs estimated:
  * `_cost_for_turn` adds $0.01 per server-side web search request. This was
    validated against ~/.claude.json's authoritative per-model costUSD, where
    it closes a 30-45% under-count on search-heavy sessions to exactly 1.000.
  * `walk_transcript` returns `parent_cost` / `subagent_cost` separately so the
    statusline can pair the authoritative parent figure with our subagent
    estimate.
  * `format_cost_with_subagents` renders `($parent + $sub~) = $total`. The parent and
    subagent figures wear the same per-figure magnitude bands (green/yellow/red);
    the trailing `= $total` is their sum and wears its OWN, higher bands (green <
    $35, yellow < $70, red >= $70) so a combined burn neither part shows alone
    still flags. The "~" is the estimate marker, and ITS color is the drift
    signal -- grey when our formula tracks the harness, else by direction +
    severity: over-estimate is cyan -> bright pink past 25% (you pay less);
    under-estimate is orange -> deep red past 25% (you may pay more). Drift never
    recolors any magnitude.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import (
    _COST_DRIFT_MAJOR_THRESHOLD,
    _COST_DRIFT_OVER_COLOR,
    _COST_DRIFT_OVER_MAJOR_COLOR,
    _COST_DRIFT_THRESHOLD,
    _COST_DRIFT_UNDER_COLOR,
    _COST_DRIFT_UNDER_MAJOR_COLOR,
    _SUBAGENT_COST_COLOR,
    _WEB_SEARCH_COST_USD,
    GREEN,
    RED,
    YELLOW,
    _cost_for_turn,
    format_cost,
    format_cost_with_subagents,
    walk_transcript,
)

_DRIFT_COLORS = (
    _COST_DRIFT_OVER_COLOR,
    _COST_DRIFT_OVER_MAJOR_COLOR,
    _COST_DRIFT_UNDER_COLOR,
    _COST_DRIFT_UNDER_MAJOR_COLOR,
)


def _assistant_line(message_id, model, usage):
    return json.dumps(
        {
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": model,
                "usage": usage,
            }
        }
    )


def _check_web_search_charge(failures):
    """Web search adds $0.01 per request on top of the token cost of a turn."""
    base = _cost_for_turn({"output_tokens": 1_000_000}, "claude-opus-4-7")
    with_ws = _cost_for_turn(
        {"output_tokens": 1_000_000, "server_tool_use": {"web_search_requests": 4}},
        "claude-opus-4-7",
    )
    if abs((with_ws - base) - 4 * _WEB_SEARCH_COST_USD) > 1e-9:
        failures.append("web search should add $0.01 per request to turn cost")


def _check_parent_subagent_split(failures):
    """walk_transcript reports parent and subagent cost separately, and zeroes
    the subagent figure (folding it into parent == total) when subagents are
    excluded."""
    with tempfile.TemporaryDirectory() as tmp:
        parent = os.path.join(tmp, "sess.jsonl")
        with open(parent, "w", encoding="utf-8") as f:
            # parent: 1M input tokens, opus -> $5.00
            f.write(
                _assistant_line("p1", "claude-opus-4-7", {"input_tokens": 1_000_000})
                + "\n"
            )
        sub_dir = os.path.join(tmp, "sess", "subagents")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "agent-x.jsonl"), "w", encoding="utf-8") as f:
            # subagent: 1M output tokens, opus -> $25.00
            f.write(
                _assistant_line("s1", "claude-opus-4-7", {"output_tokens": 1_000_000})
                + "\n"
            )

        w = walk_transcript(parent, include_subagents=True)
        if abs(w["parent_cost"] - 5.0) > 1e-6:
            failures.append(f"parent_cost should be 5.00, got {w['parent_cost']}")
        if abs(w["subagent_cost"] - 25.0) > 1e-6:
            failures.append(f"subagent_cost should be 25.00, got {w['subagent_cost']}")
        if abs(w["cost"] - 30.0) > 1e-6:
            failures.append(f"total cost should be 30.00, got {w['cost']}")

        wp = walk_transcript(parent, include_subagents=False)
        if abs(wp["subagent_cost"]) > 1e-9:
            failures.append("subagent_cost should be 0 when subagents excluded")
        if abs(wp["parent_cost"] - wp["cost"]) > 1e-9:
            failures.append("parent_cost should equal cost when subagents excluded")


def _check_magnitude_bands(failures):
    """Parent, subagent addend, and their sum each wear magnitude bands. The
    sum's bands (green <$35, yellow <$70) are higher than and independent of the
    per-figure bands (green <$25, yellow <$50)."""
    # No subagent cost -> identical to plain format_cost (unchanged).
    if format_cost_with_subagents(5.0, 5.0, 0.0) != format_cost(5.0):
        failures.append("no-subagent render should equal plain format_cost")

    green = format_cost_with_subagents(5.0, 5.0, 10.0)  # $10 -> green
    if (GREEN + "+ $10.00") not in green:
        failures.append("subagent total < $25 should be green")
    yellow = format_cost_with_subagents(5.0, 5.0, 30.0)  # $30 -> yellow
    if (YELLOW + "+ $30.00") not in yellow:
        failures.append("subagent total in [$25,$50) should be yellow")
    red = format_cost_with_subagents(5.0, 5.0, 60.0)  # $60 -> red
    if (RED + "+ $60.00") not in red:
        failures.append("subagent total >= $50 should be red")

    sum_green = format_cost_with_subagents(5.0, 5.0, 10.0)  # total $15 -> green
    if (GREEN + "= $15.00") not in sum_green:
        failures.append("summed total < $35 should be green")
    sum_yellow = format_cost_with_subagents(20.0, 20.0, 20.0)  # total $40 -> yellow
    if (YELLOW + "= $40.00") not in sum_yellow:
        failures.append("summed total in [$35,$70) should be yellow")
    sum_red = format_cost_with_subagents(40.0, 40.0, 40.0)  # total $80 -> red
    if (RED + "= $80.00") not in sum_red:
        failures.append("summed total >= $70 should be red")
    if (GREEN + "= $40.00") in sum_yellow:
        failures.append("summed total must use its own bands, not the parts' bands")

    # $60 is the lone total where the band sets disagree: yellow under the sum
    # bands (35/70), red under the per-figure bands (25/50).
    sum_disc = format_cost_with_subagents(30.0, 30.0, 30.0)  # total $60
    if (YELLOW + "= $60.00") not in sum_disc:
        failures.append("summed $60 should be yellow (sum bands 35/70)")
    if (RED + "= $60.00") in sum_disc:
        failures.append(
            "summed $60 must use sum bands (yellow), not per-figure bands (red)"
        )


def _check_drift_markers(failures):
    """The '~' marker is grey when our estimate tracks the harness, and tints by
    drift direction + severity otherwise without recoloring any magnitude. The
    no-parent case drops the redundant `= $total` segment."""
    no_parent = format_cost_with_subagents(0.0, 0.0, 10.0)
    if "+ $10.00" not in no_parent:
        failures.append("no-parent case should still surface the subagent addend")
    if "= $" in no_parent:
        failures.append("no-parent case should drop the redundant sum segment")

    green = format_cost_with_subagents(5.0, 5.0, 10.0)  # no drift -> grey '~'
    if (_SUBAGENT_COST_COLOR + "~") not in green:
        failures.append("no-drift '~' should be the neutral grey marker")
    if any(c in green for c in _DRIFT_COLORS):
        failures.append("no-drift render should not use any drift color")

    mod = _COST_DRIFT_THRESHOLD + 0.10  # moderate (between flag and major)
    big = _COST_DRIFT_MAJOR_THRESHOLD + 0.10  # way off

    over_mod = format_cost_with_subagents(5.0, 5.0 * (1 + mod), 10.0)
    if (_COST_DRIFT_OVER_COLOR + "~") not in over_mod:
        failures.append("moderate over-estimate should tint '~' cyan")
    if (GREEN + "+ $10.00") not in over_mod:
        failures.append("drift must not change the subagent magnitude color")
    if "↑" in over_mod or "↓" in over_mod:
        failures.append("drift indicator is the '~' color now, not an arrow")

    over_big = format_cost_with_subagents(5.0, 5.0 * (1 + big), 10.0)
    if (_COST_DRIFT_OVER_MAJOR_COLOR + "~") not in over_big:
        failures.append("way-off over-estimate should tint '~' bright pink")
    if _COST_DRIFT_OVER_COLOR in over_big:
        failures.append("way-off over should escalate past cyan")

    under_mod = format_cost_with_subagents(5.0, 5.0 * (1 - mod), 10.0)
    if (_COST_DRIFT_UNDER_COLOR + "~") not in under_mod:
        failures.append("moderate under-estimate should tint '~' orange")

    under_big = format_cost_with_subagents(5.0, 5.0 * (1 - big), 10.0)
    if (_COST_DRIFT_UNDER_MAJOR_COLOR + "~") not in under_big:
        failures.append("way-off under-estimate should tint '~' deep red")
    if _COST_DRIFT_UNDER_COLOR in under_big:
        failures.append("way-off under should escalate past orange")

    edge = format_cost_with_subagents(
        5.0, 5.0 * (1 + (_COST_DRIFT_THRESHOLD - 0.01)), 10.0
    )
    if any(c in edge for c in _DRIFT_COLORS):
        failures.append("drift within threshold should not tint the marker")


def check(failures):
    _check_web_search_charge(failures)
    _check_parent_subagent_split(failures)
    _check_magnitude_bands(failures)
    _check_drift_markers(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: cost field bands both figures, charges web search, tints '~' by drift direction + severity"
    )


if __name__ == "__main__":
    main()
