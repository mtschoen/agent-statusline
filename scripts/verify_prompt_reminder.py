"""Verify the progress-beacon UserPromptSubmit hook (prompt-reminder.sh) skips
harness-generated prompts (Monitor/task notifications, teammate messages) and
still reminds on owner-typed prompts.

The hook runs under ``bash`` with stdin JSON and a PATH that contains no
``agent-walker``, so the no-live-beacon fallback (emit the reminder) applies.
Skips cleanly (exit 0) when ``bash`` or ``jq`` is not installed.

Run from anywhere; locates the hook relative to this file.
"""

import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from statusline_lib.process_safe import ProcessTimeout, run_captured

HOOK = os.path.join(REPO, "skills", "progress-beacon", "hooks", "prompt-reminder.sh")
REMINDER_MARKER = "progress-beacon reminder"
SESSION_ID = "abc123"


def run_hook(payload, bash, path_dir, payload_dir):
    """Run the hook with *payload* as stdin JSON.

    ``run_captured`` gives the child no stdin, so the payload goes through a
    file that a wrapper shell redirects into the hook.
    """
    payload_path = os.path.join(payload_dir, "payload.json")
    with open(payload_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    environment = dict(os.environ)
    environment["PATH"] = path_dir
    result = run_captured(
        [bash, "-c", 'bash "$1" < "$2"', "wrapper", HOOK, payload_path],
        timeout=60,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hook exited {result.returncode}: {result.stderr}")
    return json.loads(result.stdout)


def reminded(output):
    context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
    return REMINDER_MARKER in context


def main():
    bash = shutil.which("bash")
    jq = shutil.which("jq")
    if not bash or not jq:
        print("SKIP: bash and jq are required")
        return
    # The current PATH minus every directory that provides agent-walker, so the
    # walker lookup fails and the no-live-beacon fallback (reminder) applies.
    kept = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not shutil.which("agent-walker", path=entry)
    ]
    path_dir = os.pathsep.join(kept)
    if shutil.which("agent-walker", path=path_dir):
        print("SKIP: agent-walker cannot be removed from PATH")
        return
    cases = [
        (
            "owner prompt reminds",
            {"session_id": SESSION_ID, "prompt": "why is the build red"},
            True,
        ),
        (
            "task notification skipped",
            {
                "session_id": SESSION_ID,
                "prompt": "<task-notification> <task-id>x</task-id> <summary>Monitor event: build finished</summary>",
            },
            False,
        ),
        (
            "teammate message skipped",
            {
                "session_id": SESSION_ID,
                "prompt": 'Another Claude session sent a message: <teammate-message from="x">hi</teammate-message>',
            },
            False,
        ),
        (
            "leading whitespace still skipped",
            {
                "session_id": SESSION_ID,
                "prompt": "  \n\t<task-notification> <task-id>x</task-id>",
            },
            False,
        ),
        ("missing prompt key reminds", {"session_id": SESSION_ID}, True),
        ("empty prompt reminds", {"session_id": SESSION_ID, "prompt": ""}, True),
        (
            "prefix only at start reminds",
            {"session_id": SESSION_ID, "prompt": "what does <task-notification> mean"},
            True,
        ),
    ]
    failures = []
    payload_dir = tempfile.mkdtemp()
    for name, payload, expect_reminder in cases:
        try:
            output = run_hook(payload, bash, path_dir, payload_dir)
        except (RuntimeError, ValueError, ProcessTimeout) as error:
            failures.append(f"{name}: {error}")
            continue
        if reminded(output) != expect_reminder:
            failures.append(
                f"{name}: expected reminder={expect_reminder}, got {output}"
            )
        if not expect_reminder and "hookSpecificOutput" in output:
            failures.append(f"{name}: skipped prompt must not carry hookSpecificOutput")
    shutil.rmtree(payload_dir, ignore_errors=True)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: prompt-reminder skips harness-generated prompts, reminds on owner prompts"
    )


if __name__ == "__main__":
    main()
