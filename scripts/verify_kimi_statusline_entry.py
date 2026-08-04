"""Verify kimi_statusline.py's end-to-end delegation into statusline.py
survives degenerate and wrong-typed payloads without crashing -- empty {}, a
null top-level payload, a JSON array, wrong-typed fields (contextTokens as a
string, model as an int, planMode as a string) -- and honors Kimi Code CLI's
render contract: exit 0, exactly ONE stdout line (the TUI renders only the
first line; further lines are ignored), non-empty even for {}.

Same test shape as scripts/verify_qwen_statusline_entry.py: the whole chain
is driven via subprocess rather than importing kimi_statusline directly,
with $HOME faked to a fresh temp dir so state/log writes (session-count
debounce, render-timer peak tracking, the input/error logs) never touch the
real ~/.kimi-code.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_kimi(payload_raw, tmp_home):
    env = dict(os.environ)
    env["HOME"] = tmp_home
    env["USERPROFILE"] = tmp_home
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "kimi_statusline.py")],
        input=payload_raw,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        check=False,
    )


def _expect_clean_single_line(failures, label, result):
    if result.returncode != 0:
        failures.append(
            f"{label} must not crash, got exit {result.returncode}: {result.stderr!r}"
        )
        return
    if "\n" in result.stdout:
        failures.append(
            f"{label} must render exactly one stdout line, got {result.stdout!r}"
        )
    if "[" not in result.stdout:
        failures.append(
            f"{label} should still render the prefix, got {result.stdout!r}"
        )


def _check_empty_object(failures):
    with tempfile.TemporaryDirectory() as tmp:
        _expect_clean_single_line(failures, "empty {} payload", _run_kimi("{}", tmp))


def _check_null_top_level_payload(failures):
    """A literal JSON `null` payload is valid JSON (json.loads succeeds), so
    it bypasses the parse-error except and must be handled as if it were {}."""
    with tempfile.TemporaryDirectory() as tmp:
        _expect_clean_single_line(
            failures, "null top-level payload", _run_kimi("null", tmp)
        )


def _check_non_dict_top_level_payload(failures):
    """A JSON array at the top level is also valid JSON with no dict shape."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_kimi("[]", tmp)
    if result.returncode != 0:
        failures.append(
            f"list top-level payload must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )


def _check_full_payload(failures):
    """The exact contract sample (kimi-code commit 67dd03149), flipped to
    planMode true and a non-default permissionMode so every badge renders."""
    payload = json.dumps(
        {
            "model": "K3",
            "cwd": "C:/path/to/project",
            "gitBranch": "main",
            "permissionMode": "yolo",
            "planMode": True,
            "contextUsage": 12,
            "contextTokens": 1024,
            "maxContextTokens": 8192,
            "sessionId": "abc123def456",
            "version": "0.29.2",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_kimi(payload, tmp)
    _expect_clean_single_line(failures, "full payload", result)
    for expected in ("(main)", "[abc123de]", "K3", "1.0K", "8.2K", "yolo", "PLAN"):
        if expected not in result.stdout:
            failures.append(
                f"full payload should render {expected!r}, got {result.stdout!r}"
            )


def _check_wrong_typed_fields(failures):
    """The type-confusion class: wrong-typed fields degrade to honest
    defaults at the adapter boundary instead of crashing the render (the TUI
    falls back to its built-in layout on a non-zero exit, so a crash here is
    a silent feature loss, not a visible error)."""
    payload = json.dumps(
        {
            "model": 5,
            "gitBranch": [1, 2],
            "planMode": "false",
            "contextTokens": "x",
            "maxContextTokens": "8192",
            "sessionId": None,
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_kimi(payload, tmp)
    _expect_clean_single_line(failures, "wrong-typed payload", result)
    if "PLAN" in result.stdout:
        failures.append(
            f"string planMode must not light the PLAN badge, got {result.stdout!r}"
        )


def _check_zero_max_context_tokens(failures):
    """maxContextTokens 0 (or absent context data with usage present) must
    degrade to the honest '???' denominator, not a crash or a wrong 100%."""
    payload = json.dumps({"contextTokens": 5000, "maxContextTokens": 0})
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_kimi(payload, tmp)
    _expect_clean_single_line(failures, "zero maxContextTokens", result)
    if "???" not in result.stdout:
        failures.append(
            f"zero maxContextTokens should render the '???' denominator, got "
            f"{result.stdout!r}"
        )


def check(failures):
    _check_empty_object(failures)
    _check_null_top_level_payload(failures)
    _check_non_dict_top_level_payload(failures)
    _check_full_payload(failures)
    _check_wrong_typed_fields(failures)
    _check_zero_max_context_tokens(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: kimi_statusline.py survives empty/null/malformed/wrong-typed "
        "payloads and renders exactly one line, end-to-end through the "
        "statusline.py delegation"
    )


if __name__ == "__main__":
    main()
