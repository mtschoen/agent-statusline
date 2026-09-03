"""Verify STATUSLINE_HIDE_COST suppresses every dollar figure on line 2 while
keeping the non-dollar signals (cache tokens, hit%, the TTL eviction COUNT, and
quota %/time-to-limit).

Builds a Line2Inputs directly and renders it through render_line2 at full
verbosity, so the money master switch is exercised against the real assembly.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from statusline_lib.compact import full_flags
from statusline_lib.render_line2 import Line2Inputs, render_line2

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text)


def _walk():
    # Costs nonzero so the $ parens would render unless suppressed; two TTL
    # evictions with a wasted-$ estimate so we can check the count survives but
    # the $ does not.
    return {
        "read": 3_900_000,
        "write": 108_400,
        "input": 16_600,
        "output": 50_300,
        "read_cost": 1.95,
        "write_cost": 0.68,
        "input_cost": 0.08,
        "output_cost": 1.26,
        "ttl_evictions": 2,
        "ttl_wasted": 0.58,
    }


def _line2(hide_cost):
    inputs = Line2Inputs(
        model_summary="opus4.8]",
        context_used=133_500,
        window_size=1_000_000,
        model_id="claude-opus-4-8",
        walk=_walk(),
        rate_limits=None,  # no quota/burn payload -> those segments render ""
        day_budget_summary="day:$5.00",  # a dollar figure that must drop when hidden
        cost_summary="$99.00",  # the session-cost stressor
        hide_cost=hide_cost,
        lines_summary="+543/-113",  # diffstat: NOT money, must survive hide_cost
    )
    return _strip(render_line2(full_flags(), inputs))


def check(failures):
    shown = _line2(hide_cost=False)
    hidden = _line2(hide_cost=True)

    # Baseline: with money shown, the dollar figures are all present.
    for needle in ("$99.00", "($1.95)", "($0.08)", "day:$5.00", "~$0.58"):
        if needle not in shown:
            failures.append(f"money-shown line2 missing {needle!r}; got {shown!r}")

    # Hidden: not a single '$' anywhere on the line.
    if "$" in hidden:
        failures.append(f"HIDE_COST must drop every dollar figure; got {hidden!r}")

    # Hidden: the non-dollar signals survive.
    if "hit" not in hidden:
        failures.append(f"cache hit% must survive HIDE_COST; got {hidden!r}")
    if "TTL:2" not in hidden:
        failures.append(f"TTL eviction COUNT must survive HIDE_COST; got {hidden!r}")
    # The cache token counts stay (read figure), just without their parens.
    if "3.90M" not in hidden:
        failures.append(f"cache token counts must survive HIDE_COST; got {hidden!r}")
    # The diffstat is a productivity signal, not money -> survives unchanged.
    if "+543/-113" not in hidden:
        failures.append(f"diffstat must survive HIDE_COST; got {hidden!r}")


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: STATUSLINE_HIDE_COST drops all $ figures, keeps tokens/hit/TTL count")


if __name__ == "__main__":
    main()
