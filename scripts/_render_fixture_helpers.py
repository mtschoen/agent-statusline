"""Shared fixture-corpus builder for verify_render_budget.py and
verify_cold_start.py -- both spawn real statusline.py subprocesses against a
synthetic ~/.claude and need the identical transcript-shaped fixture.
"""

import json
import os
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_fixture_home(root, n_sessions=8, turns_per_session=40):
    """Synthetic ~/.claude with enough transcript bulk to make walks real."""
    projects = os.path.join(root, ".claude", "projects", "C--fixture-proj")
    os.makedirs(projects, exist_ok=True)
    now_iso = "2026-07-11T00:00:00.000Z"
    for _ in range(n_sessions):
        sid = str(uuid.uuid4())
        lines = []
        for t in range(turns_per_session):
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": now_iso,
                        "message": {
                            "model": "claude-opus-4-8",
                            "usage": {
                                "input_tokens": 10 + t,
                                "output_tokens": 20 + t,
                                "cache_read_input_tokens": 1000,
                                "cache_creation_input_tokens": 50,
                            },
                        },
                    }
                )
            )
        with open(os.path.join(projects, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return projects
