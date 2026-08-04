"""Verify qwen_statusline.py's end-to-end delegation into statusline.py
survives degenerate and wrong-typed payloads without crashing: empty {}, a
null top-level payload, null/malformed entries nested under metrics.models,
and the type-confusion class (context_window_size as a string, display_name
as an int, metrics.models as a JSON array) fixed at the qwen adapter
boundary (statusline_lib/qwen.py) after the wave-3 canonical-model fold
(PLAN.md): qwen_statusline.py is now a thin shim that injects
`--statusline-platform qwen` and delegates into statusline.py's single
entry point, so this test drives the whole chain end-to-end via subprocess
rather than importing qwen_statusline directly.

Runs each case with $HOME faked to a fresh temp dir, so state/log writes
(session-count debounce, render-timer peak tracking, the input/error logs)
never touch the real ~/.qwen.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_qwen(payload_raw, tmp_home):
    env = dict(os.environ)
    env["HOME"] = tmp_home
    env["USERPROFILE"] = tmp_home
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "qwen_statusline.py")],
        input=payload_raw,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        check=False,
    )


def _check_empty_object(failures):
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen("{}", tmp)
    if result.returncode != 0:
        failures.append(
            f"empty {{}} payload must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )
    if "[" not in result.stdout:
        failures.append(
            f"empty {{}} payload should still render line 1, got {result.stdout!r}"
        )


def _check_null_top_level_payload(failures):
    """A literal JSON `null` payload is valid JSON (json.loads succeeds), so
    it bypasses the parse-error except and must be handled as if it were {}."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen("null", tmp)
    if result.returncode != 0:
        failures.append(
            f"null top-level payload must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )
    if "[" not in result.stdout:
        failures.append(
            f"null top-level payload should still render line 1, got {result.stdout!r}"
        )


def _check_non_dict_top_level_payload(failures):
    """A JSON array at the top level is also valid JSON with no dict shape."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen("[]", tmp)
    if result.returncode != 0:
        failures.append(
            f"list top-level payload must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )


def _check_null_model_entry(failures):
    """metrics.models.<id> = null must be skipped, not crash on .get()."""
    payload = json.dumps({"metrics": {"models": {"m1": None}}})
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(payload, tmp)
    if result.returncode != 0:
        failures.append(
            f"null model entry must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )
    if "[" not in result.stdout:
        failures.append(
            f"null model entry should still render line 1, got {result.stdout!r}"
        )


def _check_null_model_entry_mixed_with_valid(failures):
    """A null model entry alongside a valid one: the valid one's tokens must
    still be aggregated (the null entry is skipped, not fatal to the walk)."""
    payload = json.dumps(
        {
            "metrics": {
                "models": {
                    "bad": None,
                    "good": {"tokens": {"prompt": 1000, "completion": 500}},
                }
            }
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(payload, tmp)
    if result.returncode != 0:
        failures.append(
            f"mixed null/valid model entries must not crash, got exit "
            f"{result.returncode}: {result.stderr!r}"
        )
    if "1.0K" not in result.stdout:
        failures.append(
            f"valid model entry's tokens should still render alongside a null "
            f"sibling, got {result.stdout!r}"
        )


def _check_context_window_size_wrong_type(failures):
    """context_window_size as a string ("x" instead of an int) must not crash
    format_context -- the shared path badge.format_context's `window_size <=
    0` comparison would TypeError on a str/int comparison otherwise."""
    payload = json.dumps(
        {"context_window": {"context_window_size": "x", "current_usage": 5000}}
    )
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(payload, tmp)
    if result.returncode != 0:
        failures.append(
            f"string context_window_size must not crash, got exit "
            f"{result.returncode}: {result.stderr!r}"
        )
    if "???" not in result.stdout:
        failures.append(
            f"an unusable window_size should degrade to the honest '???' "
            f"denominator, got {result.stdout!r}"
        )


def _check_display_name_wrong_type(failures):
    """model.display_name as an int must not crash badge.format_model_badge
    (its mid = model_id.lower() call requires a str)."""
    payload = json.dumps({"model": {"display_name": 5}})
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(payload, tmp)
    if result.returncode != 0:
        failures.append(
            f"int display_name must not crash, got exit {result.returncode}: "
            f"{result.stderr!r}"
        )
    if "5" not in result.stdout:
        failures.append(
            f"int display_name should still render (stringified), got {result.stdout!r}"
        )


def _check_metrics_models_non_dict(failures):
    """metrics.models as a JSON array instead of an object must not crash
    _model_summaries's `models.values()` call."""
    payload = json.dumps({"metrics": {"models": [1, 2, 3]}})
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_qwen(payload, tmp)
    if result.returncode != 0:
        failures.append(
            f"metrics.models as a list must not crash, got exit "
            f"{result.returncode}: {result.stderr!r}"
        )


def check(failures):
    _check_empty_object(failures)
    _check_null_top_level_payload(failures)
    _check_non_dict_top_level_payload(failures)
    _check_null_model_entry(failures)
    _check_null_model_entry_mixed_with_valid(failures)
    _check_context_window_size_wrong_type(failures)
    _check_display_name_wrong_type(failures)
    _check_metrics_models_non_dict(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: qwen_statusline.py survives empty/null/malformed/wrong-typed "
        "degenerate payloads without crashing, end-to-end through the "
        "statusline.py delegation"
    )


if __name__ == "__main__":
    main()
