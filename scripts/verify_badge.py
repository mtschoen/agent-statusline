"""Verify context-window formatting and model-badge rendering in badge.py.

Covers:
- ctx_window_for_model: fable/mythos/[1m] -> 1M, opus/sonnet >=4.6 -> 1M,
  older opus/sonnet + claude-3 legacy + haiku -> 200K, versionless alias /
  date-fragment version / unknown family -> 0 (unknown)
- format_context: unknown-window `???` rendering (used>0) and empty-string
  guard (used<=0), color thresholds (green/yellow/orange/red),
  CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env var, show_denom/show_pct flags
- _version_for: dotted-version extraction from a model id
- _qwen_version_for: Qwen model version extraction
- _qwen_size_for: Qwen parameter-size extraction
- format_model_badge: known families (opus/sonnet/haiku/fable/qwen-coder/qwen),
  unknown-family fallback to display_name, raw id strip of "claude-" prefix,
  bare "?" when nothing is available, [1m] suffix propagation, empty-id guard

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.badge import (
    COMPACT_BUFFER_TOKENS,
    ORANGE_THRESHOLD_1M_TOKENS,
    RED_MARGIN_TOKENS,
    _qwen_size_for,
    _qwen_version_for,
    _version_for,
    ctx_window_for_model,
    format_context,
    format_model_badge,
)
from statusline_lib.base import CTX_DENOM, GREEN, ORANGE, RED, YELLOW
from statusline_lib.nudge import NUDGE_THRESHOLD_TOKENS


def _check_ctx_window_for_model(failures):
    if ctx_window_for_model("claude-fable-5") != 1_000_000:
        failures.append("ctx_window_for_model: fable model should return 1M")
    if ctx_window_for_model("claude-mythos-5") != 1_000_000:
        failures.append("ctx_window_for_model: mythos model should return 1M")
    if ctx_window_for_model("claude-opus-4[1m]") != 1_000_000:
        failures.append("ctx_window_for_model: [1m] suffix should return 1M")
    if ctx_window_for_model("claude-opus-4-8") != 1_000_000:
        failures.append("ctx_window_for_model: opus 4.8 is natively 1M")
    if ctx_window_for_model("claude-sonnet-5") != 1_000_000:
        failures.append("ctx_window_for_model: sonnet 5 is natively 1M")
    if ctx_window_for_model("claude-sonnet-4-6") != 1_000_000:
        failures.append("ctx_window_for_model: sonnet 4.6 is natively 1M")
    if ctx_window_for_model("claude-sonnet-4-5-20250929") != 200_000:
        failures.append("ctx_window_for_model: sonnet 4.5 should return 200K")
    if ctx_window_for_model("claude-opus-4-20250514") != 200_000:
        failures.append("ctx_window_for_model: opus 4.0 dated id should return 200K")
    if ctx_window_for_model("claude-3-5-sonnet-20241022") != 200_000:
        failures.append("ctx_window_for_model: claude-3 legacy ids should return 200K")
    if ctx_window_for_model("claude-haiku-4-5") != 200_000:
        failures.append("ctx_window_for_model: haiku should return 200K")
    if ctx_window_for_model("opus") != 0:
        failures.append("ctx_window_for_model: versionless alias should be unknown (0)")
    if ctx_window_for_model("claude-sonnet-20241022") != 0:
        failures.append(
            "ctx_window_for_model: date-fragment version should be unknown (0)"
        )
    if ctx_window_for_model("") != 0:
        failures.append("ctx_window_for_model: empty id should be unknown (0)")
    if ctx_window_for_model(None) != 0:
        failures.append("ctx_window_for_model: None id should be unknown (0)")
    if ctx_window_for_model("some-future-model") != 0:
        failures.append("ctx_window_for_model: unknown family should be unknown (0)")


def _check_format_context_unknown_window(failures):
    result = format_context(0, 0)
    if result != "":
        failures.append(
            f"format_context: window=0 with no usage should return '', got {result!r}"
        )
    result = format_context(100, -1)
    if "???" not in result or "100" not in result:
        failures.append(
            f"format_context: unknown window with usage should render 'used / ???', got {result!r}"
        )
    if "%" in result:
        failures.append(
            f"format_context: unknown window must not render a percentage, got {result!r}"
        )
    if CTX_DENOM not in result:
        failures.append(
            f"format_context: unknown window should use the neutral color, got {result!r}"
        )
    result_no_denom = format_context(100, 0, show_denom=False)
    if "???" in result_no_denom or " / " in result_no_denom:
        failures.append(
            f"format_context: show_denom=False should drop the '???' denominator, got {result_no_denom!r}"
        )


def _check_format_context_green(failures):
    window = 200_000
    used = 10_000
    result = format_context(used, window)
    if GREEN not in result:
        failures.append(
            f"format_context: well below threshold should be GREEN, got {result!r}"
        )


def _check_format_context_yellow(failures):
    window = 200_000
    red_tokens = max(0, max(0, window - COMPACT_BUFFER_TOKENS) - RED_MARGIN_TOKENS)
    used = max(red_tokens - 1, window // 2)
    result = format_context(used, window)
    if YELLOW not in result:
        failures.append(
            f"format_context: at yellow threshold ({used}) should be YELLOW, got {result!r}"
        )


def _check_format_context_orange(failures):
    window = 1_000_000
    red_tokens = max(0, max(0, window - COMPACT_BUFFER_TOKENS) - RED_MARGIN_TOKENS)
    used = ORANGE_THRESHOLD_1M_TOKENS
    if used >= red_tokens:
        failures.append(
            "test setup error: orange threshold >= red threshold for 1M window"
        )
        return
    result = format_context(used, window)
    if ORANGE not in result:
        failures.append(
            f"format_context: 1M model at orange threshold ({used}) should be ORANGE, got {result!r}"
        )


def _check_format_context_red(failures):
    window = 200_000
    red_tokens = max(0, max(0, window - COMPACT_BUFFER_TOKENS) - RED_MARGIN_TOKENS)
    used = red_tokens
    result = format_context(used, window)
    if RED not in result:
        failures.append(
            f"format_context: at red threshold ({used}) should be RED, got {result!r}"
        )


def _check_format_context_override(failures):
    window = 200_000
    override_pct = 80
    os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(override_pct)
    try:
        compact_tokens = int(window * override_pct / 100)
        red_tokens = max(0, compact_tokens - RED_MARGIN_TOKENS)
        result = format_context(red_tokens, window)
        if RED not in result:
            failures.append(
                f"format_context: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE red path not hit, got {result!r}"
            )
    finally:
        del os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]


def _check_format_context_override_invalid(failures):
    window = 200_000
    os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "not-a-number"
    try:
        result = format_context(0, window)
        if result == "":
            failures.append(
                "format_context: invalid override should not return empty, window is valid"
            )
    finally:
        del os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]


def _check_format_context_show_flags(failures):
    window = 200_000
    used = 1_000
    result_no_denom = format_context(used, window, show_denom=False)
    result_full = format_context(used, window)
    if " / " in result_no_denom:
        failures.append(
            f"format_context: show_denom=False should omit ' / ', got {result_no_denom!r}"
        )
    if " / " not in result_full:
        failures.append(
            f"format_context: show_denom=True should include ' / ', got {result_full!r}"
        )
    result_no_pct = format_context(used, window, show_pct=False)
    if "%" in result_no_pct:
        failures.append(
            f"format_context: show_pct=False should omit '%', got {result_no_pct!r}"
        )
    if "%" not in result_full:
        failures.append(
            f"format_context: show_pct=True should include '%', got {result_full!r}"
        )


def _check_format_context_1m_via_model_id(failures):
    # With a genuine 1M window and [1m] model_id, yellow_tokens = NUDGE_THRESHOLD_TOKENS.
    # Use used = NUDGE_THRESHOLD_TOKENS, which is well below the 1M red threshold.
    window = 1_000_000
    used = NUDGE_THRESHOLD_TOKENS
    result = format_context(used, window, model_id="claude-opus-4[1m]")
    if YELLOW not in result:
        failures.append(
            f"format_context: [1m] model_id at nudge threshold should be YELLOW, got {result!r}"
        )
    # Also verify that a non-1M 200K window uses window//2 as yellow threshold instead.
    window_200k = 200_000
    used_200k = window_200k // 2
    red_200k = max(0, max(0, window_200k - COMPACT_BUFFER_TOKENS) - RED_MARGIN_TOKENS)
    if used_200k < red_200k:
        result_200k = format_context(used_200k, window_200k)
        if YELLOW not in result_200k:
            failures.append(
                f"format_context: 200K window at window//2 should be YELLOW, got {result_200k!r}"
            )


def _check_version_for(failures):
    if _version_for("claude-opus-4-8", "opus") != "4.8":
        failures.append("_version_for: claude-opus-4-8 should yield '4.8'")
    if _version_for("claude-fable-5", "fable") != "5":
        failures.append("_version_for: claude-fable-5 should yield '5'")
    if _version_for("opus", "opus") != "":
        failures.append("_version_for: aliased id 'opus' should yield ''")
    if _version_for("claude-sonnet-4", "sonnet") != "4":
        failures.append("_version_for: claude-sonnet-4 should yield '4'")
    if _version_for("claude-haiku-3-5", "haiku") != "3.5":
        failures.append("_version_for: claude-haiku-3-5 should yield '3.5'")


def _check_qwen_version_for(failures):
    if _qwen_version_for("qwen-3-235b") != "3":
        failures.append("_qwen_version_for: qwen-3-235b should yield '3'")
    if _qwen_version_for("qwen2.5-72b") != "2.5":
        failures.append("_qwen_version_for: qwen2.5-72b should yield '2.5'")
    if _qwen_version_for("unknown-model") != "":
        failures.append("_qwen_version_for: unknown model should yield ''")


def _check_qwen_size_for(failures):
    if _qwen_size_for("qwen-3-235b") != "235b":
        failures.append("_qwen_size_for: qwen-3-235b should yield '235b'")
    if _qwen_size_for("qwen-3-32b") != "32b":
        failures.append("_qwen_size_for: qwen-3-32b should yield '32b'")
    if _qwen_size_for("qwen-3") != "":
        failures.append("_qwen_size_for: no size suffix should yield ''")


def _check_format_model_badge_empty(failures):
    if format_model_badge("") != "":
        failures.append("format_model_badge: empty id should return ''")
    if format_model_badge(None) != "":
        failures.append("format_model_badge: None id should return ''")


def _check_format_model_badge_known_families(failures):
    opus_badge = format_model_badge("claude-opus-4-8")
    if "opus" not in opus_badge:
        failures.append(f"format_model_badge: opus not in badge {opus_badge!r}")
    if "4.8" not in opus_badge:
        failures.append(
            f"format_model_badge: version 4.8 not in opus badge {opus_badge!r}"
        )
    if "\x1b[35m" not in opus_badge:
        failures.append(
            f"format_model_badge: opus should use magenta color, got {opus_badge!r}"
        )

    sonnet_badge = format_model_badge("claude-sonnet-4-5")
    if "sonnet" not in sonnet_badge:
        failures.append(f"format_model_badge: sonnet not in badge {sonnet_badge!r}")
    if "\x1b[36m" not in sonnet_badge:
        failures.append(
            f"format_model_badge: sonnet should use cyan, got {sonnet_badge!r}"
        )

    haiku_badge = format_model_badge("claude-haiku-3-5")
    if "haiku" not in haiku_badge:
        failures.append(f"format_model_badge: haiku not in badge {haiku_badge!r}")
    if "\x1b[34m" not in haiku_badge:
        failures.append(
            f"format_model_badge: haiku should use blue, got {haiku_badge!r}"
        )

    fable_badge = format_model_badge("claude-fable-5")
    if "fable" not in fable_badge:
        failures.append(f"format_model_badge: fable not in badge {fable_badge!r}")
    if "\x1b[32m" not in fable_badge:
        failures.append(
            f"format_model_badge: fable should use green, got {fable_badge!r}"
        )


def _check_format_model_badge_1m_suffix(failures):
    badge = format_model_badge("claude-opus-4[1m]")
    if "[1m]" not in badge:
        failures.append(
            f"format_model_badge: [1m] suffix should appear in badge, got {badge!r}"
        )


def _check_format_model_badge_qwen(failures):
    qwen_coder_badge = format_model_badge("qwen2.5-coder-32b")
    if "qwen-coder" not in qwen_coder_badge:
        failures.append(
            f"format_model_badge: qwen-coder label missing, got {qwen_coder_badge!r}"
        )
    if "\x1b[96m" not in qwen_coder_badge:
        failures.append(
            f"format_model_badge: qwen-coder should use bright cyan, got {qwen_coder_badge!r}"
        )

    qwen_badge = format_model_badge("qwen-3-235b")
    if "qwen" not in qwen_badge:
        failures.append(f"format_model_badge: qwen label missing, got {qwen_badge!r}")
    if "3" not in qwen_badge:
        failures.append(f"format_model_badge: qwen version missing, got {qwen_badge!r}")
    if "235b" not in qwen_badge:
        failures.append(f"format_model_badge: qwen size missing, got {qwen_badge!r}")
    if "\x1b[94m" not in qwen_badge:
        failures.append(
            f"format_model_badge: qwen should use bright blue, got {qwen_badge!r}"
        )


def _check_format_model_badge_qwen_no_size(failures):
    badge = format_model_badge("qwen-3")
    if "qwen" not in badge:
        failures.append(
            f"format_model_badge: qwen label missing for no-size model, got {badge!r}"
        )
    if "\xb7" in badge:
        failures.append(f"format_model_badge: no size -> no middle-dot, got {badge!r}")


def _check_format_model_badge_unknown_with_display_name(failures):
    badge = format_model_badge("some-unknown-model-id", display_name="Fable 5")
    if "Fable 5" not in badge:
        failures.append(
            f"format_model_badge: unknown id with display_name should show display_name, got {badge!r}"
        )
    if CTX_DENOM not in badge:
        failures.append(
            f"format_model_badge: fallback should use CTX_DENOM color, got {badge!r}"
        )


def _check_format_model_badge_unknown_strips_claude_prefix(failures):
    badge = format_model_badge("claude-future-model")
    if "future-model" not in badge:
        failures.append(
            f"format_model_badge: unknown id should strip 'claude-' prefix, got {badge!r}"
        )


def _check_format_model_badge_unknown_bare_question_mark(failures):
    badge = format_model_badge("claude-")
    if "?" not in badge:
        failures.append(
            f"format_model_badge: id that strips to empty should show '?', got {badge!r}"
        )


def main():
    failures = []

    _check_ctx_window_for_model(failures)
    _check_format_context_unknown_window(failures)
    _check_format_context_green(failures)
    _check_format_context_yellow(failures)
    _check_format_context_orange(failures)
    _check_format_context_red(failures)
    _check_format_context_override(failures)
    _check_format_context_override_invalid(failures)
    _check_format_context_show_flags(failures)
    _check_format_context_1m_via_model_id(failures)
    _check_version_for(failures)
    _check_qwen_version_for(failures)
    _check_qwen_size_for(failures)
    _check_format_model_badge_empty(failures)
    _check_format_model_badge_known_families(failures)
    _check_format_model_badge_1m_suffix(failures)
    _check_format_model_badge_qwen(failures)
    _check_format_model_badge_qwen_no_size(failures)
    _check_format_model_badge_unknown_with_display_name(failures)
    _check_format_model_badge_unknown_strips_claude_prefix(failures)
    _check_format_model_badge_unknown_bare_question_mark(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: badge.py context-window formatting and model-badge rendering all verified"
    )


if __name__ == "__main__":
    main()
