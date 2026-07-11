"""Verify qwen_statusline.py's main() survives degenerate payloads without
crashing: empty {}, a null top-level payload, and null/malformed entries
nested under metrics.models. qwen_statusline.py is adapted from statusline.py
and is the least exercised entry point, so this hardens it directly rather
than relying on the top-level try/except in __main__ to paper over crashes.

Patches _INPUT_LOG/_RAW_LOG/_ERROR_LOG to a tempdir for every run here so
this never writes to the live ~/.qwen debug dump files.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qwen_statusline as qs


def _run_main(payload_raw, tmp_dir):
    """Run qs.main() with `payload_raw` on stdin, log paths redirected into
    `tmp_dir`. Returns (stdout_text, exception_or_None)."""
    real_input_log, real_raw_log, real_error_log = (
        qs._INPUT_LOG,
        qs._RAW_LOG,
        qs._ERROR_LOG,
    )
    qs._INPUT_LOG = os.path.join(tmp_dir, "input.log")
    qs._RAW_LOG = os.path.join(tmp_dir, "input-raw.json")
    qs._ERROR_LOG = os.path.join(tmp_dir, "error.log")

    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(payload_raw)
    sys.stdout = io.StringIO()
    try:
        qs.main()
        return sys.stdout.getvalue(), None
    except Exception as exc:
        return sys.stdout.getvalue(), exc
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout
        qs._INPUT_LOG, qs._RAW_LOG, qs._ERROR_LOG = (
            real_input_log,
            real_raw_log,
            real_error_log,
        )


def _check_empty_object(failures):
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main("{}", tmp)
    if exc is not None:
        failures.append(f"empty {{}} payload must not crash main(), got {exc!r}")
    if "[" not in out:
        failures.append(f"empty {{}} payload should still render line 1, got {out!r}")


def _check_null_top_level_payload(failures):
    """A literal JSON `null` payload is valid JSON (json.loads succeeds), so
    it bypasses the parse-error except and must be handled as if it were {}."""
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main("null", tmp)
    if exc is not None:
        failures.append(f"null top-level payload must not crash main(), got {exc!r}")
    if "[" not in out:
        failures.append(
            f"null top-level payload should still render line 1, got {out!r}"
        )


def _check_non_dict_top_level_payload(failures):
    """A JSON array at the top level is also valid JSON with no dict shape."""
    with tempfile.TemporaryDirectory() as tmp:
        _out, exc = _run_main("[]", tmp)
    if exc is not None:
        failures.append(f"list top-level payload must not crash main(), got {exc!r}")


def _check_null_model_entry(failures):
    """metrics.models.<id> = null must be skipped, not crash on .get()."""
    payload = json.dumps({"metrics": {"models": {"m1": None}}})
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main(payload, tmp)
    if exc is not None:
        failures.append(f"null model entry must not crash main(), got {exc!r}")
    if "[" not in out:
        failures.append(f"null model entry should still render line 1, got {out!r}")


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
        out, exc = _run_main(payload, tmp)
    if exc is not None:
        failures.append(f"mixed null/valid model entries must not crash, got {exc!r}")
    if "1.0K" not in out:
        failures.append(
            f"valid model entry's tokens should still render alongside a null "
            f"sibling, got {out!r}"
        )


def check(failures):
    _check_empty_object(failures)
    _check_null_top_level_payload(failures)
    _check_non_dict_top_level_payload(failures)
    _check_null_model_entry(failures)
    _check_null_model_entry_mixed_with_valid(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: qwen_statusline.py main() survives empty/null/malformed degenerate "
        "payloads without crashing"
    )


if __name__ == "__main__":
    main()
