"""Verify the cost field's subagent split, web-search charge, and drift marker.

Three pieces in statusline_lib work together so the main statusline can show an
accurate cost without lying about what's measured vs estimated:
  * `_cost_for_turn` adds $0.01 per server-side web search request. This was
    validated against ~/.claude.json's authoritative per-model costUSD, where
    it closes a 30-45% under-count on search-heavy sessions to exactly 1.000.
  * `walk_transcript` returns `parent_cost` / `subagent_cost` separately so the
    statusline can pair the authoritative parent figure with our subagent
    estimate.
  * `format_cost_with_subagents` renders `$parent +$sub~`. Both numbers wear the
    same magnitude bands (green/yellow/red). The trailing "~" is the estimate
    marker, and ITS color is the drift signal -- grey when our formula tracks
    the harness, caution-orange when our PARENT estimate has diverged from the
    authoritative figure. Drift never recolors the magnitude and never shows a
    per-subagent claim (no ground truth exists for subagents).

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import (
    GREEN,
    YELLOW,
    RED,
    walk_transcript,
    format_cost,
    format_cost_with_subagents,
    _cost_for_turn,
    _WEB_SEARCH_COST_USD,
    _COST_DRIFT_THRESHOLD,
    _COST_DRIFT_COLOR,
    _SUBAGENT_COST_COLOR,
)


def _assistant_line(message_id, model, usage):
    return json.dumps(
        {"message": {"role": "assistant", "id": message_id, "model": model, "usage": usage}}
    )


def check(failures):
    # --- Web search: $0.01 per request, added on top of token cost.
    base = _cost_for_turn({"output_tokens": 1_000_000}, "claude-opus-4-7")
    with_ws = _cost_for_turn(
        {"output_tokens": 1_000_000, "server_tool_use": {"web_search_requests": 4}},
        "claude-opus-4-7",
    )
    if abs((with_ws - base) - 4 * _WEB_SEARCH_COST_USD) > 1e-9:
        failures.append("web search should add $0.01 per request to turn cost")

    # --- walk_transcript splits parent vs subagent cost.
    with tempfile.TemporaryDirectory() as tmp:
        parent = os.path.join(tmp, "sess.jsonl")
        with open(parent, "w", encoding="utf-8") as f:
            # parent: 1M input tokens, opus -> $5.00
            f.write(_assistant_line("p1", "claude-opus-4-7", {"input_tokens": 1_000_000}) + "\n")
        sub_dir = os.path.join(tmp, "sess", "subagents")
        os.makedirs(sub_dir)
        with open(os.path.join(sub_dir, "agent-x.jsonl"), "w", encoding="utf-8") as f:
            # subagent: 1M output tokens, opus -> $25.00
            f.write(_assistant_line("s1", "claude-opus-4-7", {"output_tokens": 1_000_000}) + "\n")

        w = walk_transcript(parent, include_subagents=True)
        if abs(w["parent_cost"] - 5.0) > 1e-6:
            failures.append(f"parent_cost should be 5.00, got {w['parent_cost']}")
        if abs(w["subagent_cost"] - 25.0) > 1e-6:
            failures.append(f"subagent_cost should be 25.00, got {w['subagent_cost']}")
        if abs(w["cost"] - 30.0) > 1e-6:
            failures.append(f"total cost should be 30.00, got {w['cost']}")

        # include_subagents=False -> no subagent cost, parent_cost == cost.
        wp = walk_transcript(parent, include_subagents=False)
        if abs(wp["subagent_cost"]) > 1e-9:
            failures.append("subagent_cost should be 0 when subagents excluded")
        if abs(wp["parent_cost"] - wp["cost"]) > 1e-9:
            failures.append("parent_cost should equal cost when subagents excluded")

    # --- Render: no subagent cost -> identical to plain format_cost (unchanged).
    if format_cost_with_subagents(5.0, 5.0, 0.0) != format_cost(5.0):
        failures.append("no-subagent render should equal plain format_cost")

    # --- Subagent total carries the same magnitude bands as the main cost.
    green = format_cost_with_subagents(5.0, 5.0, 10.0)   # $10 -> green
    if (GREEN + "+$10.00") not in green:
        failures.append("subagent total < $25 should be green")
    yellow = format_cost_with_subagents(5.0, 5.0, 30.0)  # $30 -> yellow
    if (YELLOW + "+$30.00") not in yellow:
        failures.append("subagent total in [$25,$50) should be yellow")
    red = format_cost_with_subagents(5.0, 5.0, 60.0)     # $60 -> red
    if (RED + "+$60.00") not in red:
        failures.append("subagent total >= $50 should be red")

    # --- The "~" is the drift signal: grey when our formula tracks the harness.
    if (_SUBAGENT_COST_COLOR + "~") not in green:
        failures.append("no-drift '~' should be the neutral grey marker")
    if _COST_DRIFT_COLOR in green:
        failures.append("no-drift render should not use the drift color anywhere")

    # --- Under drift the "~" recolors; magnitude band is untouched; no arrow.
    over = _COST_DRIFT_THRESHOLD + 0.10
    drifted = format_cost_with_subagents(5.0, 5.0 * (1 + over), 10.0)
    if (_COST_DRIFT_COLOR + "~") not in drifted:
        failures.append("drift should tint the '~' marker")
    if (GREEN + "+$10.00") not in drifted:
        failures.append("drift must not change the subagent magnitude color")
    if "↑" in drifted or "↓" in drifted:
        failures.append("drift indicator is the '~' color now, not an arrow")

    # --- Drift within threshold -> grey '~', no drift color.
    under = _COST_DRIFT_THRESHOLD - 0.01
    edge = format_cost_with_subagents(5.0, 5.0 * (1 + under), 10.0)
    if _COST_DRIFT_COLOR in edge:
        failures.append("drift within threshold should not tint the marker")


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: cost field bands both figures, charges web search, tints '~' on parent drift")


if __name__ == "__main__":
    main()
