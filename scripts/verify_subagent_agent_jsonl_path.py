"""Verify subagent_statusline.py's `_agent_jsonl_path` -- the antigravity-brain
transcript lookup named in the original task brief (diagnose+fix the
Antigravity subagent panel) but left unexercised by the first pass of this
fix. No verify script anywhere in the repo drove this function before this
file (`grep -rl "_agent_jsonl_path" scripts/` found nothing).

`_agent_jsonl_path` has its own, third, independently-implemented antigravity
detection gate (`"antigravity-cli" in parent_transcript_path or
os.environ.get("ANTIGRAVITY_AGENT") == "1"`) -- distinct from app_dir()'s and
_walker_root_list()'s env/argv-based routing. This one is self-describing: in
a real payload, `parent_transcript_path` comes straight from the harness
(`d.get("transcript_path")`, or the beacon-module fallback `_find_session_jsonl`
-- both of which point at a real Antigravity path when running under
Antigravity, containing "antigravity-cli" by construction), so it doesn't
depend on the routing bug that motivated the argv-flag fix.

Uses a synthetic `brain/<task_id>/.system_generated/logs/transcript.jsonl`
fixture under a faked HOME (never live `~/.gemini` data).

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subagent_statusline as sub

_TASK_ID = "agent-task-0001"


def _fake_expanduser_for(tmp, original):
    def fake_expanduser(path):
        return tmp if path == "~" else original(path)

    return fake_expanduser


def _brain_transcript_path(tmp, task_id):
    return os.path.join(
        tmp,
        ".gemini",
        "antigravity-cli",
        "brain",
        task_id,
        ".system_generated",
        "logs",
        "transcript.jsonl",
    )


def _with_fake_home(fn):
    """Run fn(tmp) with os.path.expanduser("~") faked to a temp dir, and
    ANTIGRAVITY_AGENT cleared so each check controls its own trigger signal."""
    original_expanduser = sub.os.path.expanduser
    original_environ = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sub.os.path.expanduser = _fake_expanduser_for(tmp, original_expanduser)
            os.environ.pop("ANTIGRAVITY_AGENT", None)
            return fn(tmp)
    finally:
        sub.os.path.expanduser = original_expanduser
        os.environ.clear()
        os.environ.update(original_environ)


def _check_direct_mapping_via_path_substring(failures):
    # Real shape: parent_transcript_path is an antigravity brain transcript
    # path (contains "antigravity-cli"), agentId == task.id maps directly.
    def run(tmp):
        transcript = _brain_transcript_path(tmp, _TASK_ID)
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        with open(transcript, "w", encoding="utf-8") as f:
            f.write("{}\n")
        parent = os.path.join(tmp, ".gemini", "antigravity-cli", "brain", "lead.jsonl")
        result = sub._agent_jsonl_path(parent, _TASK_ID)
        if result != transcript:
            failures.append(
                f"direct antigravity mapping: expected {transcript!r}, got {result!r}"
            )

    _with_fake_home(run)


def _check_direct_mapping_via_antigravity_agent_env(failures):
    # parent_transcript_path itself doesn't carry "antigravity-cli" (e.g. a
    # bare session id path), but ANTIGRAVITY_AGENT=1 is enough to trigger the
    # brain lookup on its own.
    def run(tmp):
        transcript = _brain_transcript_path(tmp, _TASK_ID)
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        with open(transcript, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.environ["ANTIGRAVITY_AGENT"] = "1"
        result = sub._agent_jsonl_path("/some/other/path.jsonl", _TASK_ID)
        if result != transcript:
            failures.append(
                f"ANTIGRAVITY_AGENT=1 mapping: expected {transcript!r}, got {result!r}"
            )

    _with_fake_home(run)


def _check_glob_fallback_on_id_drift(failures):
    # The direct agentId == task.id path is absent; the glob fallback scans
    # brain/*/.system_generated/logs/transcript.jsonl for one whose path
    # contains task_id (insurance against id-format drift between the panel
    # payload's task.id and the brain dir's own naming).
    def run(tmp):
        drifted_dir = f"session-{_TASK_ID}-suffix"
        transcript = _brain_transcript_path(tmp, drifted_dir)
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        with open(transcript, "w", encoding="utf-8") as f:
            f.write("{}\n")
        parent = os.path.join(tmp, ".gemini", "antigravity-cli", "brain", "lead.jsonl")
        result = sub._agent_jsonl_path(parent, _TASK_ID)
        if result != transcript:
            failures.append(
                f"glob fallback on id drift: expected {transcript!r}, got {result!r}"
            )

    _with_fake_home(run)


def _check_no_match_returns_empty(failures):
    # antigravity signal present, but no brain/ dir exists at all -> "" (not
    # a crash), same contract as the Claude-path branch below it.
    def run(tmp):
        parent = os.path.join(tmp, ".gemini", "antigravity-cli", "brain", "lead.jsonl")
        result = sub._agent_jsonl_path(parent, _TASK_ID)
        if result != "":
            failures.append(f"no brain/ dir should yield ''; got {result!r}")

    _with_fake_home(run)


def _check_empty_parent_path_returns_empty(failures):
    if sub._agent_jsonl_path("", _TASK_ID) != "":
        failures.append("empty parent_transcript_path should yield ''")
    if sub._agent_jsonl_path(None, _TASK_ID) != "":
        failures.append("None parent_transcript_path should yield ''")


def _check_non_antigravity_path_falls_through_to_claude_layout(failures):
    # No antigravity signal at all (no substring, no env) -> falls through
    # to the Claude-style `<parent>/subagents/agent-<id>.jsonl` layout.
    def run(tmp):
        # _agent_jsonl_path derives sub_dir via `base + "/subagents"` (a
        # literal string concat, not os.path.join) -- match that exactly so
        # the expected path's separators agree with what the function builds.
        parent = os.path.join(tmp, "projects", "proj", "lead.jsonl")
        base, _ext = os.path.splitext(parent)
        sub_dir = base + "/subagents"
        os.makedirs(sub_dir, exist_ok=True)
        direct = os.path.join(sub_dir, f"agent-{_TASK_ID}.jsonl")
        with open(direct, "w", encoding="utf-8") as f:
            f.write("{}\n")
        result = sub._agent_jsonl_path(parent, _TASK_ID)
        if result != direct:
            failures.append(
                f"non-antigravity fallback: expected {direct!r}, got {result!r}"
            )

    _with_fake_home(run)


def check(failures):
    _check_direct_mapping_via_path_substring(failures)
    _check_direct_mapping_via_antigravity_agent_env(failures)
    _check_glob_fallback_on_id_drift(failures)
    _check_no_match_returns_empty(failures)
    _check_empty_parent_path_returns_empty(failures)
    _check_non_antigravity_path_falls_through_to_claude_layout(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: _agent_jsonl_path resolves the antigravity-brain lookup correctly")


if __name__ == "__main__":
    main()
