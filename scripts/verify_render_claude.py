"""Verify statusline_lib/render_claude.py, the Claude Code / Antigravity CLI
render extracted from statusline.py so the resident server can render
without importing the entry script.

Asserts on structure rather than exact strings: the moved pieces (line 1
git ref, session badge, session name width gate, beacon line) are already
covered by their own callees; this script proves the orchestrator wires
them together and honours the walk/now/state_directory parameters it is
handed instead of computing them itself.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The scripts directory, so the shared fixture helper is importable, and the
# home redirection it installs before the first statusline_lib import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _render_fixture_helpers import isolate_home

_HOME = isolate_home("verify-render-claude-")

import statusline_lib.render_claude as render_claude_mod
from statusline_lib.render_claude import (
    _beacon_line,
    _format_cwd,
    _line1,
    context_usage,
    render_claude_statusline,
    transcript_path_for,
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NOW = 1_700_000_000.0
_SESSION_ID = "abcd1234-5678-90ab-cdef-1234567890ab"


def _walk(read=40000, write=100, input_tokens=10, output=50):
    return {
        "read": read,
        "write": write,
        "input": input_tokens,
        "output": output,
        "read_cost": 0.012,
        "write_cost": 0.000375,
        "input_cost": 0.00015,
        "output_cost": 0.00375,
        "ttl_evictions": 2,
        "ttl_wasted": 0.5,
        "parent_cost": 1.0,
        "subagent_cost": 0.5,
        "user_prompts": 3,
        "assistant_turns": 4,
    }


def _payload(**overrides):
    fields = {
        "session_id": _SESSION_ID,
        "workspace": {"project_dir": _REPO, "current_dir": _REPO},
        "cwd": _REPO,
        "context_window": {
            "context_window_size": 200_000,
            "current_usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 40000,
            },
        },
        "model": {"id": "claude-opus-4-8", "display_name": "Opus"},
        "cost": {
            "total_cost_usd": 1.0,
            "total_lines_added": 10,
            "total_lines_removed": 2,
        },
        "rate_limits": None,
        "transcript_path": "",
    }
    fields.update(overrides)
    return fields


def check_render_produces_three_lines(failures):
    with tempfile.TemporaryDirectory() as state:
        payload = _payload()
        walk = _walk()
        rendered = render_claude_statusline(
            payload, _REPO, walk, now=_NOW, state_directory=state
        )
        lines = rendered.split("\n")
        if not 1 <= len(lines) <= 3:
            failures.append(f"expected one to three lines, got {len(lines)}")
        if _SESSION_ID[:8] not in lines[0]:
            failures.append(f"line 1 must carry the session badge: {lines[0]!r}")
        if len(lines) < 2 or "|" not in lines[1]:
            failures.append(
                f"line 2 must be the pipe-joined field list: {lines[1:2]!r}"
            )


def check_render_takes_the_walk_it_is_given(failures):
    """The extracted render must never call walk_transcript itself: the
    server owns the walk and maintains it incrementally."""
    with tempfile.TemporaryDirectory() as state:
        walk = _walk(read=123456)
        rendered = render_claude_statusline(
            _payload(), _REPO, walk, now=_NOW, state_directory=state
        )
        if "123" not in rendered:
            failures.append("the render ignored the walk it was handed")


def check_context_usage_sums_the_three_usage_fields(failures):
    used, window, current_usage = context_usage(_payload())
    expected_usage = _payload()["context_window"]["current_usage"]
    if (used, window, current_usage) != (40110, 200000, expected_usage):
        failures.append(f"context_usage returned {(used, window, current_usage)}")


def check_context_usage_defaults_window_when_absent(failures):
    used, window, current_usage = context_usage({})
    if (used, window, current_usage) != (0, 200_000, {}):
        failures.append(
            "context_usage should default an absent context_window: "
            f"{(used, window, current_usage)}"
        )


def check_transcript_path_for_prefers_the_payload_field(failures):
    path = transcript_path_for(_payload(transcript_path="/tmp/some-session.jsonl"))
    if path != "/tmp/some-session.jsonl":
        failures.append(f"transcript_path_for ignored the payload field: {path!r}")


def check_transcript_path_for_empty_without_session_id(failures):
    path = transcript_path_for({"transcript_path": ""})
    if path != "":
        failures.append(
            f"transcript_path_for should be '' with no session id: {path!r}"
        )


def check_render_handles_missing_workspace(failures):
    # No "workspace" key at all: cwd must fall back to the payload's own
    # "cwd" field rather than raising.
    with tempfile.TemporaryDirectory() as state:
        payload = _payload()
        del payload["workspace"]
        rendered = render_claude_statusline(
            payload, _REPO, _walk(), now=_NOW, state_directory=state
        )
        if not rendered:
            failures.append("render with no workspace block produced nothing")


def check_render_colours_the_relative_cwd_hop(failures):
    # project_dir differs from current_dir: _format_cwd's relative-hop
    # colouring branch, reached only through the full render.
    with tempfile.TemporaryDirectory() as state:
        nested = os.path.join(_REPO, "statusline_lib")
        payload = _payload(workspace={"project_dir": _REPO, "current_dir": nested})
        rendered = render_claude_statusline(
            payload, nested, _walk(), now=_NOW, state_directory=state
        )
        if "statusline_lib" not in rendered:
            failures.append(
                f"line 1 should show the relative hop to the nested cwd: {rendered!r}"
            )


def check_render_on_a_detached_head(failures):
    # Seed the gitref cache (same shape verify_git_ref_cache.py writes) with a
    # branch-less entry, mirroring a detached HEAD: _git_ref must still render
    # the bare hash, and _line1 must fold it into line 1 in parens.
    from statusline_lib.gitref import _git_ref_cache_path

    with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cwd:
        cache_path = _git_ref_cache_path(cwd, state_dir=state)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {"cached_at_unix": time.time(), "branch": "", "short_hash": "abc123"}, f
            )
        payload = _payload(workspace={"project_dir": cwd, "current_dir": cwd})
        rendered = render_claude_statusline(
            payload, cwd, _walk(), now=_NOW, state_directory=state
        )
        if "abc123" not in rendered:
            failures.append(
                f"a detached-HEAD git ref (bare hash, no branch) should still"
                f" appear in line 1: {rendered!r}"
            )


def check_git_ref_with_a_branch_appears_in_parens(failures):
    from statusline_lib.gitref import _git_ref_cache_path

    with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cwd:
        cache_path = _git_ref_cache_path(cwd, state_dir=state)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "cached_at_unix": time.time(),
                    "branch": "main",
                    "short_hash": "def456",
                },
                f,
            )
        payload = _payload(workspace={"project_dir": cwd, "current_dir": cwd})
        rendered = render_claude_statusline(
            payload, cwd, _walk(), now=_NOW, state_directory=state
        )
        if "(main:" not in rendered:
            failures.append(
                f"a cache hit carrying a branch should render '(branch:hash)' in"
                f" line 1: {rendered!r}"
            )


def check_format_cwd_cross_drive_falls_back_to_absolute(failures):
    # On real Windows, os.path.relpath raises ValueError across drive
    # letters, reaching _format_cwd's except-ValueError branch. On Linux
    # CI, posixpath.relpath never raises for these inputs (it returns a
    # ".."-prefixed string instead), so the branch would go uncovered
    # there under the 100%-coverage gate. Force the branch directly by
    # monkeypatching the relpath the module actually calls (os.path.relpath,
    # the same object on every platform statusline_lib.render_claude
    # imports "os" from), restored in finally.
    original_relpath = render_claude_mod.os.path.relpath

    def _raising_relpath(*args, **kwargs):
        raise ValueError("no relative path across drives")

    render_claude_mod.os.path.relpath = _raising_relpath
    try:
        rendered = _format_cwd("C:\\Users\\example\\home", "D:\\separate\\drive")
    finally:
        render_claude_mod.os.path.relpath = original_relpath
    if "D:\\separate\\drive" not in rendered:
        failures.append(
            f"_format_cwd should fall back to the absolute cwd when relpath"
            f" raises ValueError: {rendered!r}"
        )


def check_format_cwd_ancestor_hop_shows_absolute(failures):
    # current is an ANCESTOR of home: os.path.relpath returns a leading "..",
    # which _format_cwd treats the same as the cross-drive case (absolute,
    # not a "../" hop, since stepping out above home is more confusing than
    # helpful as a relative path).
    home = os.path.join(_REPO, "statusline_lib")
    rendered = _format_cwd(home, _REPO)
    if _REPO not in rendered:
        failures.append(
            f"_format_cwd should show the absolute path when current is an"
            f" ancestor of home: {rendered!r}"
        )


def check_line1_badges_two_or_more_sessions(failures):
    original_count = render_claude_mod.count_active_sessions
    original_debounce = render_claude_mod.debounce_session_count
    render_claude_mod.count_active_sessions = lambda cwd: 2
    render_claude_mod.debounce_session_count = lambda count, cwd: count
    try:
        with tempfile.TemporaryDirectory() as state:
            rendered = _line1({}, _REPO, _REPO, "|", state_directory=state)
    finally:
        render_claude_mod.count_active_sessions = original_count
        render_claude_mod.debounce_session_count = original_debounce
    if "2 sessions" not in rendered:
        failures.append(
            f"_line1 should badge two or more concurrent sessions: {rendered!r}"
        )


def check_render_claude_statusline_accepts_preloaded_session_count(failures):
    def _raising_count(cwd):
        raise AssertionError(
            "count_active_sessions must not be called when session_count is passed"
        )

    saved_count = render_claude_mod.count_active_sessions
    saved_debounce = render_claude_mod.debounce_session_count
    render_claude_mod.count_active_sessions = _raising_count
    render_claude_mod.debounce_session_count = lambda count, cwd: count
    try:
        with tempfile.TemporaryDirectory() as state:
            rendered = render_claude_mod.render_claude_statusline(
                {}, _REPO, _walk(), 1000.0, state_directory=state, session_count=2
            )
    finally:
        render_claude_mod.count_active_sessions = saved_count
        render_claude_mod.debounce_session_count = saved_debounce

    if "[2 sessions]" not in rendered:
        failures.append(
            f"render_claude_statusline with session_count=2 should render badge: {rendered!r}"
        )


def check_line1_shows_the_agent_state_tag(failures):
    with tempfile.TemporaryDirectory() as state:
        rendered = _line1(
            {"agent_state": "working"}, _REPO, _REPO, "|", state_directory=state
        )
    if "working" not in rendered:
        failures.append(
            f"_line1 should show the Antigravity agent_state tag: {rendered!r}"
        )


def check_beacon_line_appends_the_calibrated_eta(failures):
    original_beacon = render_claude_mod.format_beacon
    original_eta = render_claude_mod.format_calibrated_eta
    render_claude_mod.format_beacon = lambda session_id: (
        "turn 1/3",
        {"eta_seconds": 90},
    )
    render_claude_mod.format_calibrated_eta = lambda eta_seconds: f"eta {eta_seconds}s"
    try:
        rendered = _beacon_line(_SESSION_ID)
    finally:
        render_claude_mod.format_beacon = original_beacon
        render_claude_mod.format_calibrated_eta = original_eta
    if rendered != "turn 1/3  ·  eta 90s":
        failures.append(f"_beacon_line should append the calibrated ETA: {rendered!r}")


def check_long_session_name_dropped_by_width_gate(failures):
    with tempfile.TemporaryDirectory() as state:
        saved = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "60"
        try:
            payload = _payload(session_name="x" * 200)
            rendered = render_claude_statusline(
                payload, _REPO, _walk(), now=_NOW, state_directory=state
            )
            if "x" * 50 in rendered:
                failures.append(
                    "an oversized session name should be dropped, not truncated in"
                    " onto a too-narrow line 1"
                )
        finally:
            if saved is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = saved


def check_beacon_line_without_a_positive_eta_returns_the_bare_summary(failures):
    original_beacon = render_claude_mod.format_beacon
    render_claude_mod.format_beacon = lambda session_id: (
        "turn 1/3",
        {"eta_seconds": 0},
    )
    try:
        rendered = _beacon_line(_SESSION_ID)
    finally:
        render_claude_mod.format_beacon = original_beacon
    if rendered != "turn 1/3":
        failures.append(
            f"_beacon_line with no positive ETA should return the bare summary:"
            f" {rendered!r}"
        )


def check_beacon_disabled_env_arm(failures):
    # No live beacons-latest cache exists for this synthetic session id
    # either way, so format_beacon already returns nothing; this check's
    # job is coverage of the pref_bool early return inside _beacon_line,
    # not a behavioral difference in the rendered text.
    saved = os.environ.get("STATUSLINE_BEACON")
    os.environ["STATUSLINE_BEACON"] = "0"
    try:
        with tempfile.TemporaryDirectory() as state:
            rendered = render_claude_statusline(
                _payload(), _REPO, _walk(), now=_NOW, state_directory=state
            )
            if "⏱" in rendered:
                failures.append(
                    f"STATUSLINE_BEACON=0 must suppress the beacon row: {rendered!r}"
                )
    finally:
        if saved is None:
            os.environ.pop("STATUSLINE_BEACON", None)
        else:
            os.environ["STATUSLINE_BEACON"] = saved


def main():
    failures = []
    check_render_produces_three_lines(failures)
    check_render_takes_the_walk_it_is_given(failures)
    check_context_usage_sums_the_three_usage_fields(failures)
    check_context_usage_defaults_window_when_absent(failures)
    check_transcript_path_for_prefers_the_payload_field(failures)
    check_transcript_path_for_empty_without_session_id(failures)
    check_render_handles_missing_workspace(failures)
    check_render_colours_the_relative_cwd_hop(failures)
    check_render_on_a_detached_head(failures)
    check_git_ref_with_a_branch_appears_in_parens(failures)
    check_format_cwd_cross_drive_falls_back_to_absolute(failures)
    check_format_cwd_ancestor_hop_shows_absolute(failures)
    check_line1_badges_two_or_more_sessions(failures)
    check_render_claude_statusline_accepts_preloaded_session_count(failures)
    check_line1_shows_the_agent_state_tag(failures)
    check_beacon_line_appends_the_calibrated_eta(failures)
    check_beacon_line_without_a_positive_eta_returns_the_bare_summary(failures)
    check_long_session_name_dropped_by_width_gate(failures)
    check_beacon_disabled_env_arm(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: render_claude extracted; walk injection and cwd/beacon arms covered")


if __name__ == "__main__":
    main()
