"""Verify statusline_lib/qwen.py's payload adapter: the type-confusion
guards (_safe_str, _safe_int), _model_summaries's non-dict guards, and
render_qwen_statusline's end-to-end normalization of Qwen's payload shape.

Split from scripts/verify_qwen_render.py (which only covers the pure
formatters) to keep that file focused; this one covers the wave-3
canonical-model fold's actual adapter logic (PLAN.md), added alongside the
2026-07-19 type-confusion hardening (context_window_size as a string,
display_name as an int, metrics.models as a JSON array).

count_active_sessions/debounce_session_count/format_render_suffix are
monkeypatched at the statusline_lib.qwen module level (the names it imported
into its own namespace) rather than exercised for real, so this never
touches the live ~/.qwen or ~/.claude session-count/debounce/render-timer
state files.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.qwen as qwen_module
from statusline_lib.qwen import (
    _model_summaries,
    _safe_int,
    _safe_str,
    render_qwen_statusline,
)


def _check_safe_str(failures):
    if _safe_str(None) != "":
        failures.append(f"_safe_str(None) should be '', got {_safe_str(None)!r}")
    if _safe_str(5) != "5":
        failures.append(f"_safe_str(5) should stringify to '5', got {_safe_str(5)!r}")
    if _safe_str("abc") != "abc":
        failures.append(
            f"_safe_str('abc') should pass through, got {_safe_str('abc')!r}"
        )


def _check_safe_int(failures):
    if _safe_int(None) != 0:
        failures.append(f"_safe_int(None) should default to 0, got {_safe_int(None)!r}")
    if _safe_int(None, default=7) != 7:
        failures.append(
            f"_safe_int(None, default=7) should return the custom default, got "
            f"{_safe_int(None, default=7)!r}"
        )
    if _safe_int("42") != 42:
        failures.append(
            f"_safe_int('42') should convert to 42, got {_safe_int('42')!r}"
        )
    if _safe_int("x") != 0:
        failures.append(
            f"_safe_int('x') (ValueError path) should degrade to 0, got {_safe_int('x')!r}"
        )
    if _safe_int([1, 2]) != 0:
        failures.append(
            f"_safe_int([1, 2]) (TypeError path) should degrade to 0, got "
            f"{_safe_int([1, 2])!r}"
        )


def _check_model_summaries_non_dict_models(failures):
    result = _model_summaries([1, 2, 3])
    if result != ("", "", "", ""):
        failures.append(
            f"_model_summaries with a JSON array should return all-empty, got {result!r}"
        )


def _check_model_summaries_non_dict_model_entry(failures):
    result = _model_summaries({"m1": "not-a-dict"})
    if result != ("", "", "", ""):
        failures.append(
            f"_model_summaries with a non-dict model entry should skip it, got {result!r}"
        )


def _check_model_summaries_non_dict_tokens_and_api(failures):
    result = _model_summaries({"m1": {"tokens": "bad", "api": "bad"}})
    if result != ("", "", "", ""):
        failures.append(
            f"_model_summaries with non-dict tokens/api should coerce to empty, got "
            f"{result!r}"
        )


def _check_model_summaries_valid_with_cache(failures):
    models = {
        "m1": {
            "tokens": {
                "prompt": 1000,
                "completion": 200,
                "cached": 300,
                "thoughts": 10,
            },
            "api": {"total_requests": 2, "total_errors": 0, "total_latency_ms": 500},
        }
    }
    cache_summary, tokens_summary, thinking_summary, api_summary = _model_summaries(
        models
    )
    if not cache_summary:
        failures.append("expected a non-empty cache_summary when cached>0 and prompt>0")
    if not tokens_summary:
        failures.append("expected a non-empty tokens_summary for a valid model entry")
    if not thinking_summary:
        failures.append("expected a non-empty thinking_summary when thoughts>0")
    if not api_summary:
        failures.append("expected a non-empty api_summary when total_requests>0")


class _PatchedQwenSessions:
    """Swap statusline_lib.qwen's imported count_active_sessions /
    debounce_session_count / format_render_suffix names for fakes, so
    render_qwen_statusline's session-badge and render-timer-suffix branches
    are exercised without touching real state files."""

    def __init__(self, n_sessions, render_suffix):
        self._n_sessions = n_sessions
        self._render_suffix = render_suffix
        self._originals = {}

    def __enter__(self):
        self._originals["count_active_sessions"] = qwen_module.count_active_sessions
        self._originals["debounce_session_count"] = qwen_module.debounce_session_count
        self._originals["format_render_suffix"] = qwen_module.format_render_suffix
        qwen_module.count_active_sessions = lambda cwd: self._n_sessions
        qwen_module.debounce_session_count = lambda raw_count, cwd: raw_count
        qwen_module.format_render_suffix = lambda session_id: self._render_suffix
        return self

    def __exit__(self, *_exc_info):
        qwen_module.count_active_sessions = self._originals["count_active_sessions"]
        qwen_module.debounce_session_count = self._originals["debounce_session_count"]
        qwen_module.format_render_suffix = self._originals["format_render_suffix"]


def _check_render_qwen_statusline_full_payload(failures):
    payload = {
        "workspace": {"current_dir": "/tmp"},
        "git": {"branch": "main"},
        "context_window": {"context_window_size": 128000, "current_usage": 500},
        "model": {"display_name": "qwen-3-8b"},
        "metrics": {
            "models": {"m1": {"tokens": {"prompt": 100, "completion": 50}}},
            "files": {"total_lines_added": 5, "total_lines_removed": 0},
        },
        "vim": {"mode": "NORMAL"},
    }
    with _PatchedQwenSessions(n_sessions=3, render_suffix="ui 1.00ms peak 2.00ms"):
        line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
    if "(main)" not in line1:
        failures.append(f"line1 should show the git branch, got {line1!r}")
    if "[3 sessions]" not in line1:
        failures.append(f"line1 should show the multi-session badge, got {line1!r}")
    if "ui 1.00ms peak 2.00ms" not in line2:
        failures.append(f"line2 should include the render-timer suffix, got {line2!r}")
    if "VIM:NORMAL" not in line2:
        failures.append(f"line2 should show the vim mode, got {line2!r}")
    if "+5" not in line2:
        failures.append(f"line2 should show the files summary, got {line2!r}")


def _check_render_qwen_statusline_wrong_typed_containers(failures):
    """Every payload-container field wrong-typed at once (a list or string
    instead of an object) -- each must degrade to {} rather than crash on the
    isinstance guard. With no usable data anywhere, line2 degrades all the
    way to "" -- honest absence, not a crash."""
    payload = {
        "git": [1, 2],
        "context_window": [1, 2, 3],
        "model": "oops",
        "metrics": "oops",
        "vim": "oops",
    }
    with _PatchedQwenSessions(n_sessions=1, render_suffix=""):
        line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
    if not line1:
        failures.append("line1 should still render on an all-wrong-typed payload")
    if line2 != "":
        failures.append(
            f"an all-wrong-typed payload with no other data should degrade to an "
            f"empty line2, got {line2!r}"
        )


def _check_render_qwen_statusline_string_context_window_size(failures):
    """context_window_size as a string ("x") alongside a real current_usage:
    the shared badge.format_context's `window_size <= 0` comparison must not
    TypeError on a str, and the unusable window degrades to the honest '???'
    denominator instead of asserting a wrong number."""
    payload = {"context_window": {"context_window_size": "x", "current_usage": 500}}
    with _PatchedQwenSessions(n_sessions=1, render_suffix=""):
        _line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
    if "???" not in line2:
        failures.append(
            f"a string context_window_size should degrade to the honest '???' "
            f"denominator, got {line2!r}"
        )


def _check_render_qwen_statusline_non_dict_files(failures):
    """metrics.files as a JSON array (metrics itself a valid dict) must
    degrade to {} rather than crash format_qwen_files's .get() calls."""
    payload = {"metrics": {"files": [1, 2, 3]}}
    with _PatchedQwenSessions(n_sessions=1, render_suffix=""):
        _line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
    if line2 != "":
        failures.append(
            f"metrics.files as a list with no other data should degrade to an "
            f"empty line2, got {line2!r}"
        )


def check(failures):
    _check_safe_str(failures)
    _check_safe_int(failures)
    _check_model_summaries_non_dict_models(failures)
    _check_model_summaries_non_dict_model_entry(failures)
    _check_model_summaries_non_dict_tokens_and_api(failures)
    _check_model_summaries_valid_with_cache(failures)
    _check_render_qwen_statusline_full_payload(failures)
    _check_render_qwen_statusline_wrong_typed_containers(failures)
    _check_render_qwen_statusline_string_context_window_size(failures)
    _check_render_qwen_statusline_non_dict_files(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: statusline_lib/qwen.py's adapter (_safe_str/_safe_int/_model_summaries/"
        "render_qwen_statusline) normalizes Qwen's payload shape and survives "
        "wrong-typed fields"
    )


if __name__ == "__main__":
    main()
