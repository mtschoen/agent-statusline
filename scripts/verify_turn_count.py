"""Verify the line-1 turn counter: typed user prompts are counted from the
parent transcript only, assistant turns feed the fallback label, and the
statusline.py appender renders the segment.

Builds real temp JSONL transcripts and runs walk_transcript over them (same
fixture style as verify_cache_cost_split.py), so every guard in
_accumulate_user_prompt is exercised: tool results (all-`tool_result` block
lists), isMeta / isSidechain bookkeeping, empty content, missing content,
non-text blocks, and <local-command-...> wrappers must NOT count; plain typed
strings and content-block lists carrying typed `text` blocks must. A wiring
smoke test calls render_claude_statusline directly against a fixture
transcript so removing the format_turn_count call or its _line1 wiring
cannot leave the suite green -- the resident server reaches that same
function, not statusline.py, which is a thin client wrapper now (PLAN.md's
resident-server redesign).

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import re
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# The scripts directory, so the shared fixture helper is importable, and the
# home redirection it installs before the first statusline_lib import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _render_fixture_helpers import isolate_home

_HOME = isolate_home("verify-turn-count-")

from statusline_lib import format_turn_count
from statusline_lib.cost import walk_transcript
from statusline_lib.render_claude import _append_turn_count, render_claude_statusline

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text)


def _write_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _assistant(mid):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "id": mid,
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }


def _prompt(text, **extra):
    entry = {"type": "user", "message": {"role": "user", "content": text}}
    entry.update(extra)
    return entry


def _check_parent_prompt_counting(failures):
    tmp = tempfile.mkdtemp(prefix="turn-count-parent-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(
        parent,
        [
            _prompt("fix the flaky test"),  # typed prompt: counts
            _assistant("m1"),
            _prompt("now run the suite"),  # typed prompt: counts
            _assistant("m1"),  # duplicate message id: does not double-count
            _assistant("m2"),
            # Typed prompt as a content-block list (Claude Code emits this
            # shape too - a list is not synonymous with a tool result).
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "ship it"}],
                },
            },
            _assistant("m3"),
            # Tool result: user entry with an all-tool_result block list -
            # not a typed prompt.
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ok"}],
                },
            },
            # A text-block list whose text is empty/whitespace: not a prompt.
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "  "},
                        {"type": "image", "source": {}},
                        "not-a-dict-block",
                    ],
                },
            },
            # Harness bookkeeping: isMeta caveat and sidechain lines.
            _prompt("Caveat: local commands below", isMeta=True),
            _prompt("sidechain chatter", isSidechain=True),
            # Empty / whitespace-only content is not a prompt.
            _prompt(""),
            _prompt("   "),
            # Missing content (no `message.content` key at all): not a prompt.
            {"type": "user", "message": {"role": "user"}},
            # Local slash-command output wrapper, not typed input.
            _prompt("<local-command-stdout>done</local-command-stdout>"),
            # A non-user, non-assistant line (e.g. summary): ignored by both.
            {"type": "summary", "summary": "session so far"},
        ],
    )
    walk = walk_transcript(parent, include_subagents=False)
    if walk["user_prompts"] != 3:
        failures.append(f"user_prompts {walk['user_prompts']!r} != 3")
    if walk["assistant_turns"] != 3:
        failures.append(f"assistant_turns {walk['assistant_turns']!r} != 3")


def _check_subagent_prompts_excluded(failures):
    tmp = tempfile.mkdtemp(prefix="turn-count-subs-")
    parent = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(parent, [_prompt("parent turn"), _assistant("p1")])
    sub_dir = os.path.join(tmp, "sess", "subagents")
    os.makedirs(sub_dir)
    _write_jsonl(
        os.path.join(sub_dir, "agent-a1.jsonl"),
        [_prompt("subagent task prompt"), _assistant("s1")],
    )
    walk = walk_transcript(parent, include_subagents=True)
    if walk["user_prompts"] != 1:
        failures.append(
            f"subagent task prompt must not count as a user turn; got "
            f"user_prompts {walk['user_prompts']!r}"
        )
    if walk["assistant_turns"] != 2:
        failures.append(
            f"assistant_turns should include subagent steps; got "
            f"{walk['assistant_turns']!r}"
        )


def _check_missing_transcript(failures):
    walk = walk_transcript(
        os.path.join(tempfile.mkdtemp(prefix="turn-count-missing-"), "nope.jsonl")
    )
    if walk["user_prompts"] != 0 or walk["assistant_turns"] != 0:
        failures.append(f"missing transcript should zero both counts; got {walk!r}")


def _check_format_turn_count(failures):
    cases = [
        ((3, 10), "3 turns"),
        ((1, 1), "1 turn"),
        ((0, 5), "5 steps"),
        ((0, 1), "1 step"),
        ((None, 0), ""),
        ((0, None), ""),
    ]
    for (prompts, steps), expected in cases:
        rendered = _strip(format_turn_count(prompts, steps))
        if rendered != expected:
            failures.append(
                f"format_turn_count({prompts!r}, {steps!r}) -> {rendered!r}, "
                f"expected {expected!r}"
            )


def _check_append_turn_count(failures):
    rendered = _strip(_append_turn_count("base", "7 turns"))
    if rendered != "base 7 turns":
        failures.append(f"_append_turn_count should append; got {rendered!r}")
    noop = _append_turn_count("base", "")
    if noop != "base":
        failures.append(f"_append_turn_count with '' should be a no-op; got {noop!r}")


def _check_wiring_reaches_line1(failures):
    """render_claude_statusline must actually call _append_turn_count while
    building line 1. The in-process checks above stop at the
    _append_turn_count helper itself; without this, deleting the
    format_turn_count call or its _line1 wiring from render_claude.py would
    stay green. Calls render_claude_statusline directly (the function the
    resident server reaches, not statusline.py, which is a thin client
    wrapper now -- PLAN.md's resident-server redesign) against a fixture
    transcript walked for real.
    """
    tmp = tempfile.mkdtemp(prefix="turn-count-wiring-")
    transcript = os.path.join(tmp, "sess.jsonl")
    _write_jsonl(
        transcript,
        [
            _prompt("first typed prompt"),
            _assistant("m1"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "second typed prompt"}],
                },
            },
            _assistant("m2"),
        ],
    )
    payload = {
        "session_id": "turn-count-wiring",
        "transcript_path": transcript,
        "cwd": tmp,
        "workspace": {"current_dir": tmp, "project_dir": tmp},
        "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
    }
    walk = walk_transcript(transcript, include_subagents=True)
    rendered = render_claude_statusline(payload, tmp, walk, time.time())
    first_line = _strip(rendered.splitlines()[0]) if rendered else ""
    if "2 turns" not in first_line:
        failures.append(f"line 1 should contain '2 turns'; got {first_line!r}")


def check(failures):
    _check_parent_prompt_counting(failures)
    _check_subagent_prompts_excluded(failures)
    _check_missing_transcript(failures)
    _check_format_turn_count(failures)
    _check_append_turn_count(failures)
    _check_wiring_reaches_line1(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: turn counting, format_turn_count labels, and line-1 appender")


if __name__ == "__main__":
    main()
