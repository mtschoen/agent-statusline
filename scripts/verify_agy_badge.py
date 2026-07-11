"""Verify the Gemini (Antigravity CLI) model-badge extension in badge.py:
flash/pro get distinct colors, the parenthesized reasoning tier renders as a
separate suffix tag, and none of the existing Claude/Qwen badge behavior
(covered by scripts/verify_badge.py) is disturbed.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.badge import (
    _GEMINI_VARIANT_COLORS,
    _gemini_tier_for,
    _gemini_variant_for,
    _gemini_version_for,
    format_model_badge,
)
from statusline_lib.base import CTX_DENOM, RESET


def _check_gemini_version_for(failures):
    if _gemini_version_for("gemini 3.5 flash (high)") != "3.5":
        failures.append(
            "_gemini_version_for: 'gemini 3.5 flash (high)' should yield '3.5'"
        )
    if _gemini_version_for("gemini-2 pro") != "2":
        failures.append("_gemini_version_for: hyphenated id should still extract '2'")
    if _gemini_version_for("gemini flash") != "":
        failures.append("_gemini_version_for: no version present should yield ''")


def _check_gemini_variant_for(failures):
    if _gemini_variant_for("gemini 3.5 flash (high)") != "flash":
        failures.append("_gemini_variant_for: should detect 'flash'")
    if _gemini_variant_for("gemini 3.0 pro") != "pro":
        failures.append("_gemini_variant_for: should detect 'pro'")
    if _gemini_variant_for("gemini 3.0 ultra") != "":
        failures.append("_gemini_variant_for: unrecognized variant should yield ''")


def _check_gemini_tier_for(failures):
    if _gemini_tier_for("Gemini 3.5 Flash (High)") != "High":
        failures.append("_gemini_tier_for: should extract 'High' with original casing")
    if _gemini_tier_for("Gemini 3.5 Flash") != "":
        failures.append("_gemini_tier_for: no parens should yield ''")
    if _gemini_tier_for(None) != "":
        failures.append("_gemini_tier_for: None should yield '' (no crash)")


def _check_format_model_badge_gemini_flash(failures):
    badge = format_model_badge("Gemini 3.5 Flash (High)", "Gemini 3.5 Flash (High)")
    if "gemini-flash" not in badge:
        failures.append(
            f"format_model_badge: gemini-flash label missing, got {badge!r}"
        )
    if "3.5" not in badge:
        failures.append(f"format_model_badge: gemini version missing, got {badge!r}")
    if _GEMINI_VARIANT_COLORS["flash"] not in badge:
        failures.append(
            f"format_model_badge: gemini flash should use its own color, got {badge!r}"
        )
    if "(High)" not in badge:
        failures.append(
            f"format_model_badge: reasoning tier tag missing, got {badge!r}"
        )
    if CTX_DENOM not in badge:
        failures.append(
            f"format_model_badge: tier tag should use the mauve tag color, got {badge!r}"
        )
    # The tier must be a separate tag, not fused into the badge label/name.
    if "flash3.5(high)" in badge.lower().replace(" ", ""):
        failures.append(
            f"format_model_badge: tier should not be fused into the badge name, got {badge!r}"
        )


def _check_format_model_badge_gemini_pro(failures):
    badge = format_model_badge("gemini-2.0-pro")
    if "gemini-pro" not in badge:
        failures.append(f"format_model_badge: gemini-pro label missing, got {badge!r}")
    if _GEMINI_VARIANT_COLORS["pro"] not in badge:
        failures.append(
            f"format_model_badge: gemini pro should use its own color, got {badge!r}"
        )
    if _GEMINI_VARIANT_COLORS["flash"] in badge:
        failures.append(
            f"format_model_badge: pro badge must not use the flash color, got {badge!r}"
        )


def _check_format_model_badge_gemini_unknown_variant(failures):
    badge = format_model_badge("Gemini 3.0 Ultra")
    if "gemini" not in badge:
        failures.append(
            f"format_model_badge: unrecognized-variant gemini id should still say gemini, got {badge!r}"
        )
    if "gemini-flash" in badge or "gemini-pro" in badge:
        failures.append(
            f"format_model_badge: unrecognized variant must not claim flash/pro, got {badge!r}"
        )


def _check_format_model_badge_gemini_no_tier(failures):
    badge = format_model_badge("Gemini 3.5 Flash")
    if "(" in badge:
        failures.append(
            f"format_model_badge: no reasoning tier present should omit the tag, got {badge!r}"
        )
    if RESET not in badge:
        failures.append("format_model_badge: colored output must reset")


def _check_claude_and_qwen_untouched(failures):
    # Spot-check a couple of non-gemini families to guard against the new
    # gemini branch leaking into existing matches.
    if "opus" not in format_model_badge("claude-opus-4-8"):
        failures.append("format_model_badge: claude opus badge regressed")
    if "qwen-coder" not in format_model_badge("qwen2.5-coder-32b"):
        failures.append("format_model_badge: qwen-coder badge regressed")


def check(failures):
    _check_gemini_version_for(failures)
    _check_gemini_variant_for(failures)
    _check_gemini_tier_for(failures)
    _check_format_model_badge_gemini_flash(failures)
    _check_format_model_badge_gemini_pro(failures)
    _check_format_model_badge_gemini_unknown_variant(failures)
    _check_format_model_badge_gemini_no_tier(failures)
    _check_claude_and_qwen_untouched(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: Gemini model-badge extension verified, Claude/Qwen badges unaffected")


if __name__ == "__main__":
    main()
