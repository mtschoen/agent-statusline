"""Entry-level regression tests for the agy-specific dispatch inside
statusline.py::main()/_render_line2 -- the cross-field interactions unit
tests on statusline_lib.agy alone cannot exercise.

Drives statusline.main() in-process (stdin/stdout patched, log paths
redirected into a tempdir), mirroring verify_qwen_statusline_entry.py's
pattern for the qwen entry point.

Covers the reviewer's reproduction of the cache-truthfulness Critical: the
per-turn cache fallback (statusline_lib.agy.format_agy_cache) must fire ONLY
for agy-identified payloads (`product == "antigravity"`), never as a generic
"the transcript walk found nothing" fallback -- a Claude Code payload with an
unreadable transcript_path must render no cache field at all (the pre-existing
honest "no data" signal), not a plausible-looking per-turn number in the
session-cumulative field's usual visual form. Also covers the quota
per-horizon selection rule end-to-end, since format_agy_quota's own unit tests
(scripts/verify_agy_quota.py) can't prove statusline.py actually wires the raw
`quota` payload block through unmodified.

Run from anywhere; imports from schoen-claude-status by path.
"""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline


def _run_main(payload, tmp_dir):
    """Run statusline.main() with `payload` (a dict) on stdin, log paths
    redirected into `tmp_dir`. Returns (stdout_text, exception_or_None)."""
    real_input_log, real_error_log = statusline._INPUT_LOG, statusline._ERROR_LOG
    statusline._INPUT_LOG = os.path.join(tmp_dir, "input.log")
    statusline._ERROR_LOG = os.path.join(tmp_dir, "error.log")

    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    real_columns = os.environ.get("COLUMNS")
    os.environ.pop("COLUMNS", None)  # deterministic: no host-terminal width leaks in
    try:
        statusline.main()
        return sys.stdout.getvalue(), None
    except Exception as exc:
        return sys.stdout.getvalue(), exc
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout
        statusline._INPUT_LOG, statusline._ERROR_LOG = real_input_log, real_error_log
        if real_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = real_columns


_UNREADABLE_TRANSCRIPT = "C:/does/not/exist/nowhere.jsonl"

_CLAUDE_CURRENT_USAGE = {
    "input_tokens": 4917,
    "output_tokens": 254,
    "cache_creation_input_tokens": 1000,
    "cache_read_input_tokens": 40000,
}


def _claude_payload(**overrides):
    payload = {
        "session_id": "claude-session-1",
        "workspace": {"project_dir": "", "current_dir": ""},
        "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
        "context_window": {
            "context_window_size": 200_000,
            "current_usage": _CLAUDE_CURRENT_USAGE,
        },
        "cost": {"total_cost_usd": 1.23},
        "rate_limits": {
            "five_hour": {"used_percentage": 12.0, "resets_at": 9_999_999_999},
        },
        "transcript_path": _UNREADABLE_TRANSCRIPT,
    }
    payload.update(overrides)
    return payload


def _agy_payload(**overrides):
    payload = {
        "session_id": "agy-session-1",
        "product": "antigravity",
        "workspace": {"project_dir": "", "current_dir": ""},
        "model": {
            "id": "Gemini 3.5 Flash (High)",
            "display_name": "Gemini 3.5 Flash (High)",
        },
        "context_window": {
            "context_window_size": 1_048_576,
            "current_usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 2000,
            },
        },
        "quota": {
            "gemini-5h": {
                "remaining_fraction": 0.5,
                "reset_time": "2099-01-01T00:00:00Z",
                "reset_in_seconds": 999_999_999,
            },
        },
        "transcript_path": _UNREADABLE_TRANSCRIPT,
    }
    payload.update(overrides)
    return payload


def _check_claude_payload_broken_walk_renders_no_cache_field(failures):
    # Critical reproduction: a Claude Code payload whose transcript walk finds
    # nothing must render NO cache field at all, not the agy per-turn
    # fallback's plausible-looking numbers.
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main(_claude_payload(), tmp)
    if exc is not None:
        failures.append(f"Claude payload with broken walk must not crash, got {exc!r}")
    if "turn" in out:
        failures.append(
            f"Claude payload must never render the agy 'turn' cache marker, got {out!r}"
        )
    if "hit" in out:
        failures.append(
            f"Claude payload with a broken transcript walk must render no cache "
            f"field at all (honest 'no data'), got {out!r}"
        )


def _check_agy_payload_renders_turn_marked_cache(failures):
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main(_agy_payload(), tmp)
    if exc is not None:
        failures.append(f"agy payload must not crash, got {exc!r}")
    if "turn" not in out:
        failures.append(
            f"agy payload should render the per-turn cache fallback with its "
            f"'turn' marker, got {out!r}"
        )
    if "hit" not in out:
        failures.append(
            f"agy payload's per-turn cache field should show hit%, got {out!r}"
        )


def _check_agy_quota_never_hides_hottest_5h_window(failures):
    # End-to-end version of the reviewer's exact hiding scenario: gemini-5h is
    # the hottest 5h window (95%) but 3p-weekly is the hottest weekly window
    # (96%) -- neither must be hidden behind the other family's pairing.
    quota = {
        "gemini-5h": {
            "remaining_fraction": 0.05,
            "reset_time": "2099-01-01T00:00:00Z",
            "reset_in_seconds": 999_999_999,
        },
        "gemini-weekly": {
            "remaining_fraction": 0.9,
            "reset_time": "2099-01-01T00:00:00Z",
            "reset_in_seconds": 999_999_999,
        },
        "3p-5h": {
            "remaining_fraction": 0.95,
            "reset_time": "2099-01-01T00:00:00Z",
            "reset_in_seconds": 999_999_999,
        },
        "3p-weekly": {
            "remaining_fraction": 0.04,
            "reset_time": "2099-01-01T00:00:00Z",
            "reset_in_seconds": 999_999_999,
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        out, exc = _run_main(_agy_payload(quota=quota), tmp)
    if exc is not None:
        failures.append(f"agy quota payload must not crash, got {exc!r}")
    if "5h: " not in out:
        failures.append(f"expected a 5h: quota slot in the render, got {out!r}")
    five_hour_part = out.split("5h: ", 1)[-1].split("wk:")[0]
    if "95" not in five_hour_part:
        failures.append(
            f"5h slot must show gemini-5h's 95% (the hottest 5h window), got {out!r}"
        )


def check(failures):
    _check_claude_payload_broken_walk_renders_no_cache_field(failures)
    _check_agy_payload_renders_turn_marked_cache(failures)
    _check_agy_quota_never_hides_hottest_5h_window(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: statusline.py gates the agy cache fallback to agy payloads and "
        "never hides the hottest per-horizon quota window"
    )


if __name__ == "__main__":
    main()
