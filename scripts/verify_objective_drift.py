"""Verify `_compute_objective_drift` classifies drift from observed elapsed
+ current eta vs original begin eta, not from the agent's self-report.

Replaces the old beacon["drift"] passthrough — historical data showed
agents never self-reported moderate/material even when reality diverged
2-10× from the begin estimate.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import _compute_objective_drift


def iso(dt):
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def run_case(name, begin_minutes_ago, begin_eta_seconds, current_eta_seconds, expected):
    now = datetime.now(UTC).replace(tzinfo=None)
    begin_ts = iso(now - timedelta(minutes=begin_minutes_ago))
    got = _compute_objective_drift(begin_ts, begin_eta_seconds, current_eta_seconds)
    ok = got == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(
        f"  begin {begin_minutes_ago}m ago, begin_eta={begin_eta_seconds}s, current_eta={current_eta_seconds}s"
    )
    print(f"  got={got!r}  expect={expected!r}")
    return ok


def main():
    results = []

    # Fresh begin, on-track: 0 elapsed + 300 eta = 300 / 300 = 1.0× -> nominal
    results.append(run_case("on-track at begin", 0, 300, 300, "nominal"))

    # 2 min elapsed, 3 min remaining, original was 5 min: (120+180)/300 = 1.0× -> nominal
    results.append(run_case("on-track mid-lifecycle", 2, 300, 180, "nominal"))

    # 5 min elapsed, 3 min remaining, original was 5 min: (300+180)/300 = 1.6× -> moderate
    results.append(run_case("slipping moderately", 5, 300, 180, "moderate"))

    # 8 min elapsed, 3 min remaining, original was 5 min: (480+180)/300 = 2.2× -> material
    results.append(run_case("slipping materially", 8, 300, 180, "material"))

    # 35 min elapsed, original was 60 min, claims 5 min remaining: (2100+300)/3600 = 0.67×
    # — would be nominal by ratio, but elapsed > 30min -> material
    results.append(
        run_case("long absolute elapsed forces material", 35, 3600, 300, "material")
    )

    # The lowballed-restated pattern: begin said 5 min, 25 min elapsed, agent
    # still says "5 min remaining". (1500+300)/300 = 6× -> material.
    results.append(run_case("lowballed-restated lies caught", 25, 300, 300, "material"))

    # Missing begin eta -> fall back to nominal (don't flash red on no data)
    results.append(
        run_case("no begin eta -> nominal fallback", 10, None, 60, "nominal")
    )

    # Begin eta = 0 -> fall back to nominal (avoid div-by-zero red)
    results.append(run_case("zero begin eta -> nominal fallback", 10, 0, 60, "nominal"))

    if all(results):
        print("\nAll cases pass.")
        return 0
    print("\nSome cases failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
