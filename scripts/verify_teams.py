"""Verify the agent-teams teammate summary line.

`subagentStatusLine` only covers classic Task-tool subagents -- teammates
spawned under `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` never appear in that
payload (see statusline_lib/teams.py docstring). `format_teammates` reads
`~/.claude/teams/<name>/config.json` and each teammate's own transcript JSONL
directly instead, so this exercises that path end to end with synthetic
fixtures -- never live `~/.claude` data or a live clock.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import (
    GREEN,
    IDLE_THRESHOLD_SECONDS,
    _is_active,
    _load_team_config,
    _team_name_for_session,
    _teammate_jsonl,
    format_teammates,
)

_SESSION_ID = "b213becd-6513-43f3-95a1-e4d51c47cb39"
_TEAM_NAME = "session-b213becd"


def _assistant_line(model, usage):
    return json.dumps(
        {"message": {"role": "assistant", "id": "m1", "model": model, "usage": usage}}
    )


def _write_config(claude_dir, members):
    team_dir = os.path.join(claude_dir, "teams", _TEAM_NAME)
    os.makedirs(team_dir, exist_ok=True)
    with open(os.path.join(team_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"name": _TEAM_NAME, "members": members}, f)


def _lead_member():
    return {
        "agentId": f"team-lead@{_TEAM_NAME}",
        "name": "team-lead",
        "agentType": "team-lead",
    }


def _check_team_name_derivation(failures):
    if _team_name_for_session(_SESSION_ID) != _TEAM_NAME:
        failures.append("team name should be 'session-' + first 8 chars of session id")
    if _team_name_for_session("") != "":
        failures.append("empty session id should yield no team name")
    if _team_name_for_session(None) != "":
        failures.append("None session id should yield no team name")


def _check_no_config(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        result = format_teammates(_SESSION_ID, "", claude_dir, now=1000.0)
        if result != "":
            failures.append("missing team config should render nothing")


def _check_malformed_config(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        team_dir = os.path.join(claude_dir, "teams", _TEAM_NAME)
        os.makedirs(team_dir)
        with open(os.path.join(team_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        if _load_team_config(claude_dir, _TEAM_NAME) is not None:
            failures.append("malformed config.json should load as None, not raise")
        result = format_teammates(_SESSION_ID, "", claude_dir, now=1000.0)
        if result != "":
            failures.append("malformed team config should render nothing")


def _check_lead_only(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        _write_config(claude_dir, [_lead_member()])
        result = format_teammates(_SESSION_ID, "", claude_dir, now=1000.0)
        if result != "":
            failures.append("a team with only the lead should render nothing")


def _check_active_teammate_with_cost(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        _write_config(
            claude_dir,
            [_lead_member(), {"name": "watchme", "model": "sonnet"}],
        )
        transcript = os.path.join(claude_dir, "projects", "proj", "sess.jsonl")
        os.makedirs(os.path.dirname(transcript))
        sub_dir = os.path.join(os.path.dirname(transcript), "sess", "subagents")
        os.makedirs(sub_dir)
        jsonl = os.path.join(sub_dir, "agent-awatchme-abc123.jsonl")
        with open(jsonl, "w", encoding="utf-8") as f:
            # opus 1M input tokens -> $5.00, but model here is sonnet: 1M
            # input @ $3/Mtok -> $3.00.
            f.write(
                _assistant_line("claude-sonnet-5", {"input_tokens": 1_000_000}) + "\n"
            )
        os.utime(jsonl, (990.0, 990.0))  # 10s before "now" -> active

        result = format_teammates(_SESSION_ID, transcript, claude_dir, now=1000.0)
        if "teammates: " not in result:
            failures.append("result should be prefixed with 'teammates: '")
        if "watchme" not in result:
            failures.append("result should include the teammate's name")
        if GREEN + "●" not in result:
            failures.append("a recently-touched teammate should render the active icon")
        if "$3.00" not in result:
            failures.append("cost should be derived from the teammate's own transcript")
        if "team-lead" in result:
            failures.append("the lead itself must not appear as a teammate row")


def _check_idle_teammate_no_cost(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        _write_config(claude_dir, [{"name": "researcher", "model": "haiku"}])
        transcript = os.path.join(claude_dir, "projects", "proj", "sess.jsonl")
        os.makedirs(os.path.dirname(transcript))
        sub_dir = os.path.join(os.path.dirname(transcript), "sess", "subagents")
        os.makedirs(sub_dir)
        jsonl = os.path.join(sub_dir, "agent-aresearcher-def456.jsonl")
        with open(jsonl, "w", encoding="utf-8") as f:
            f.write(_assistant_line("claude-haiku-4-5", {}) + "\n")
        stale = 1000.0 - IDLE_THRESHOLD_SECONDS - 1
        os.utime(jsonl, (stale, stale))

        result = format_teammates(_SESSION_ID, transcript, claude_dir, now=1000.0)
        if GREEN + "●" in result:
            failures.append(
                "a stale teammate transcript should not render the active icon"
            )
        if "$" in result:
            failures.append("a zero-cost turn should omit the cost segment")


def _check_no_transcript_yet(failures):
    """A teammate that hasn't produced a transcript file yet (or whose
    subagents dir doesn't exist) should still render, just without cost and as
    idle -- not crash."""
    with tempfile.TemporaryDirectory() as claude_dir:
        _write_config(claude_dir, [{"name": "brandnew", "model": "opus"}])
        result = format_teammates(_SESSION_ID, "", claude_dir, now=1000.0)
        if "brandnew" not in result:
            failures.append(
                "a teammate with no transcript yet should still render by name"
            )
        if "$" in result:
            failures.append("no transcript means no cost segment")


def _check_jsonl_substring_matching(failures):
    with tempfile.TemporaryDirectory() as sub_dir:
        with open(os.path.join(sub_dir, "agent-aother-111.jsonl"), "w") as f:
            f.write("")
        with open(os.path.join(sub_dir, "agent-awatchme-222.jsonl"), "w") as f:
            f.write("")
        found = _teammate_jsonl(sub_dir, "watchme")
        if not found.endswith("agent-awatchme-222.jsonl"):
            failures.append(
                "should match the file whose name contains the teammate name"
            )
        if _teammate_jsonl(sub_dir, "nomatch") != "":
            failures.append("no matching file should return an empty path")
        if _teammate_jsonl(sub_dir, "") != "":
            failures.append("empty name should return an empty path")
        if _teammate_jsonl(os.path.join(sub_dir, "missing"), "watchme") != "":
            failures.append("a nonexistent subagents dir should return an empty path")


def _check_is_active_edge_cases(failures):
    if _is_active("", 1000.0):
        failures.append("empty path should never be active")
    if _is_active("/does/not/exist.jsonl", 1000.0):
        failures.append("a nonexistent file should never be active")


def _check_empty_team_name_short_circuits_config_load(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        if _load_team_config(claude_dir, "") is not None:
            failures.append("an empty team name should short-circuit to None")


def _check_nameless_members_render_nothing(failures):
    with tempfile.TemporaryDirectory() as claude_dir:
        _write_config(claude_dir, [{"agentType": "general-purpose"}])
        result = format_teammates(_SESSION_ID, "", claude_dir, now=1000.0)
        if result != "":
            failures.append("a non-lead member with no name should not render a row")


def check(failures):
    _check_team_name_derivation(failures)
    _check_no_config(failures)
    _check_malformed_config(failures)
    _check_lead_only(failures)
    _check_active_teammate_with_cost(failures)
    _check_idle_teammate_no_cost(failures)
    _check_no_transcript_yet(failures)
    _check_jsonl_substring_matching(failures)
    _check_is_active_edge_cases(failures)
    _check_empty_team_name_short_circuits_config_load(failures)
    _check_nameless_members_render_nothing(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: teammate summary line derives team name, reads config.json + per-teammate "
        "transcripts, and renders active/idle icons + cost without touching live ~/.claude data"
    )


if __name__ == "__main__":
    main()
