"""Eval runner for progress-beacon. v1: scaffolding only.

The actual grader is a v2 follow-up (see evals/README.md). v1 ships the
case definitions so reviewers can see what the skill is meant to produce
and so the grader has a stable contract to wire against.
"""

from __future__ import annotations

import argparse
import sys

EVAL_CASES = [
    {
        "id": "trigger-trivial",
        "prompt": "what is 2+2?",
        "expect_beacon": False,
        "rationale": "single-line answer; skill should stay silent",
    },
    {
        "id": "trigger-nontrivial",
        "prompt": "refactor auth.py to use JWT, run the tests, commit",
        "expect_beacon": True,
        "expect_kind_sequence": ["begin", "report", "end"],
        "rationale": "multi-step implementation work; should emit a full lifecycle",
    },
    {
        "id": "drift-engineered",
        "prompt": "fix the bug in this function - there are five subtle bugs hiding",
        "expect_beacon": True,
        "expect_loud_note": True,
        "rationale": "actual scope materially exceeds initial estimate; the agent's own (elapsed+eta)/original>=2 math should cross the material threshold and fire the loud note",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list eval cases and exit")
    args = parser.parse_args()
    if args.list:
        for c in EVAL_CASES:
            print(f"{c['id']:24s}  {c['prompt']}")
        return 0
    print("eval runner v1 is scaffolding-only -- see evals/README.md", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
