"""Verify statusline_lib/qwen.py formatters: cache, tokens, thinking, API stats,
and file-change rendering. Asserts exact expected substrings and empty-string
returns for all guard branches, covering all reachable lines in qwen.py.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.base import (
    CACHE_READ,
    CACHE_WRITE,
    CTX_DENOM,
    GREEN,
    RED,
    RESET,
    YELLOW,
)
from statusline_lib.qwen import (
    format_qwen_api_stats,
    format_qwen_cache,
    format_qwen_files,
    format_qwen_thinking,
    format_qwen_tokens,
)


def _check_cache_empty_when_no_cached(failures):
    result = format_qwen_cache(0, 1000)
    if result != "":
        failures.append(f"cache with cached=0 must return empty string, got {result!r}")


def _check_cache_empty_when_cached_none(failures):
    result = format_qwen_cache(None, 1000)
    if result != "":
        failures.append(
            f"cache with cached=None must return empty string, got {result!r}"
        )


def _check_cache_empty_when_cached_negative(failures):
    result = format_qwen_cache(-1, 1000)
    if result != "":
        failures.append(
            f"cache with cached=-1 must return empty string, got {result!r}"
        )


def _check_cache_renders_cached_and_hit_no_fake_write(failures):
    """Qwen exposes no priced cache-write side (no write cost premium exists
    the way it does for Claude), so the column must show cached tokens + hit%
    only -- no CACHE_WRITE-colored figure standing in for a cost that isn't
    real. See format_qwen_cache's docstring for the pricing citation."""
    result = format_qwen_cache(730_000, 1_000_000)
    if CACHE_READ not in result:
        failures.append(f"cache result must contain CACHE_READ color, got {result!r}")
    if CACHE_WRITE in result:
        failures.append(
            f"cache result must NOT contain CACHE_WRITE (no priced write side "
            f"for Qwen), got {result!r}"
        )
    if "73%" not in result:
        failures.append(f"cache result must contain '73%', got {result!r}")
    if RESET not in result:
        failures.append(f"cache result must contain RESET, got {result!r}")
    if "730.0K" not in result:
        failures.append(
            f"cache result must contain cached size '730.0K', got {result!r}"
        )
    if result.count("/") != 1:
        failures.append(
            f"cache result must be a single 'cached / hit%' pair (one '/'), "
            f"got {result!r}"
        )


def _check_cache_zero_prompt_no_divide(failures):
    result = format_qwen_cache(100, 0)
    if result == "":
        failures.append("cache with cached>0 and prompt=0 must not return empty string")
    if "0%" not in result:
        failures.append(f"cache with prompt=0 must show 0% hit rate, got {result!r}")


def _check_tokens_empty_when_none(failures):
    result = format_qwen_tokens(None)
    if result != "":
        failures.append(f"tokens with None must return empty string, got {result!r}")


def _check_tokens_empty_when_empty_dict(failures):
    result = format_qwen_tokens({})
    if result != "":
        failures.append(
            f"tokens with empty dict must return empty string, got {result!r}"
        )


def _check_tokens_renders_prompt_and_completion(failures):
    result = format_qwen_tokens(
        {"prompt": 2_440_000, "completion": 35_200, "total": 0, "cached": 0}
    )
    if GREEN not in result:
        failures.append(f"tokens result must contain GREEN for prompt, got {result!r}")
    if YELLOW not in result:
        failures.append(
            f"tokens result must contain YELLOW for completion, got {result!r}"
        )
    if "2.44M" not in result:
        failures.append(
            f"tokens result must contain formatted prompt '2.44M', got {result!r}"
        )
    if "35.2K" not in result:
        failures.append(
            f"tokens result must contain formatted completion '35.2K', got {result!r}"
        )
    if RESET not in result:
        failures.append(f"tokens result must contain RESET, got {result!r}")


def _check_tokens_only_prompt_no_completion(failures):
    result = format_qwen_tokens({"prompt": 1000, "completion": 0})
    if GREEN not in result:
        failures.append(f"tokens with only prompt must contain GREEN, got {result!r}")
    if YELLOW in result:
        failures.append(
            f"tokens with completion=0 must not contain YELLOW, got {result!r}"
        )


def _check_tokens_only_completion_no_prompt(failures):
    result = format_qwen_tokens({"prompt": 0, "completion": 5000})
    if YELLOW not in result:
        failures.append(
            f"tokens with only completion must contain YELLOW, got {result!r}"
        )
    if GREEN in result:
        failures.append(f"tokens with prompt=0 must not contain GREEN, got {result!r}")


def _check_tokens_empty_when_both_zero(failures):
    result = format_qwen_tokens({"prompt": 0, "completion": 0})
    if result != "":
        failures.append(
            f"tokens with both zero must return empty string, got {result!r}"
        )


def _check_thinking_empty_when_none(failures):
    result = format_qwen_thinking(None)
    if result != "":
        failures.append(f"thinking with None must return empty string, got {result!r}")


def _check_thinking_empty_when_zero_thoughts(failures):
    result = format_qwen_thinking({"prompt": 1000, "thoughts": 0})
    if result != "":
        failures.append(
            f"thinking with thoughts=0 must return empty string, got {result!r}"
        )


def _check_thinking_renders_when_positive(failures):
    result = format_qwen_thinking({"prompt": 1000, "thoughts": 10_100})
    if CTX_DENOM not in result:
        failures.append(f"thinking result must contain CTX_DENOM color, got {result!r}")
    if "thk" not in result:
        failures.append(f"thinking result must contain 'thk', got {result!r}")
    if "10.1K" not in result:
        failures.append(
            f"thinking result must contain formatted thoughts '10.1K', got {result!r}"
        )
    if RESET not in result:
        failures.append(f"thinking result must contain RESET, got {result!r}")


def _check_thinking_has_space_after_thk(failures):
    """Docstring promises '(thk NNNK)' -- a space between 'thk' and the number."""
    result = format_qwen_thinking({"prompt": 1000, "thoughts": 10_100})
    if "thk 10.1K" not in result:
        failures.append(
            f"thinking result must contain a space after 'thk' ('thk 10.1K'), got {result!r}"
        )


def _check_api_empty_when_none(failures):
    result = format_qwen_api_stats(None)
    if result != "":
        failures.append(f"api_stats with None must return empty string, got {result!r}")


def _check_api_empty_when_empty_dict(failures):
    result = format_qwen_api_stats({})
    if result != "":
        failures.append(
            f"api_stats with empty dict must return empty string, got {result!r}"
        )


def _check_api_empty_when_zero_requests(failures):
    result = format_qwen_api_stats(
        {"total_requests": 0, "total_errors": 0, "total_latency_ms": 1000}
    )
    if result != "":
        failures.append(
            f"api_stats with total_requests=0 must return empty string, got {result!r}"
        )


def _check_api_renders_requests_only(failures):
    result = format_qwen_api_stats(
        {"total_requests": 5, "total_errors": 0, "total_latency_ms": 0}
    )
    if "5req" not in result:
        failures.append(f"api_stats must contain '5req', got {result!r}")
    if RED in result:
        failures.append(
            f"api_stats with no errors must not contain RED, got {result!r}"
        )


def _check_api_renders_errors(failures):
    result = format_qwen_api_stats(
        {"total_requests": 10, "total_errors": 2, "total_latency_ms": 0}
    )
    if RED not in result:
        failures.append(f"api_stats with errors must contain RED, got {result!r}")
    if "2err" not in result:
        failures.append(f"api_stats with errors must contain '2err', got {result!r}")
    if RESET not in result:
        failures.append(f"api_stats with errors must contain RESET, got {result!r}")


def _check_api_renders_latency(failures):
    result = format_qwen_api_stats(
        {"total_requests": 10, "total_errors": 0, "total_latency_ms": 5000}
    )
    if "5.0s" not in result:
        failures.append(
            f"api_stats must contain formatted latency '5.0s', got {result!r}"
        )


def _check_api_renders_all_fields(failures):
    result = format_qwen_api_stats(
        {"total_requests": 10, "total_errors": 3, "total_latency_ms": 8500}
    )
    if "10req" not in result:
        failures.append(f"api_stats all-fields must contain '10req', got {result!r}")
    if "3err" not in result:
        failures.append(f"api_stats all-fields must contain '3err', got {result!r}")
    if "8.5s" not in result:
        failures.append(f"api_stats all-fields must contain '8.5s', got {result!r}")


def _check_files_empty_when_none(failures):
    result = format_qwen_files(None)
    if result != "":
        failures.append(f"files with None must return empty string, got {result!r}")


def _check_files_empty_when_empty_dict(failures):
    result = format_qwen_files({})
    if result != "":
        failures.append(
            f"files with empty dict must return empty string, got {result!r}"
        )


def _check_files_empty_when_both_zero(failures):
    result = format_qwen_files({"total_lines_added": 0, "total_lines_removed": 0})
    if result != "":
        failures.append(
            f"files with both zero must return empty string, got {result!r}"
        )


def _check_files_renders_added(failures):
    result = format_qwen_files({"total_lines_added": 120, "total_lines_removed": 0})
    if GREEN not in result:
        failures.append(f"files added must contain GREEN, got {result!r}")
    if "+120" not in result:
        failures.append(f"files added must contain '+120', got {result!r}")
    if RED in result:
        failures.append(f"files with no removals must not contain RED, got {result!r}")


def _check_files_renders_removed(failures):
    result = format_qwen_files({"total_lines_added": 0, "total_lines_removed": 30})
    if RED not in result:
        failures.append(f"files removed must contain RED, got {result!r}")
    if "-30" not in result:
        failures.append(f"files removed must contain '-30', got {result!r}")
    if GREEN in result:
        failures.append(
            f"files with no additions must not contain GREEN, got {result!r}"
        )


def _check_files_renders_added_and_removed(failures):
    result = format_qwen_files({"total_lines_added": 120, "total_lines_removed": 30})
    if "+120" not in result:
        failures.append(f"files +/- must contain '+120', got {result!r}")
    if "-30" not in result:
        failures.append(f"files +/- must contain '-30', got {result!r}")
    if "/" not in result:
        failures.append(f"files +/- must be joined with '/', got {result!r}")
    if RESET not in result:
        failures.append(f"files +/- must contain RESET, got {result!r}")


def main():
    failures = []

    _check_cache_empty_when_no_cached(failures)
    _check_cache_empty_when_cached_none(failures)
    _check_cache_empty_when_cached_negative(failures)
    _check_cache_renders_cached_and_hit_no_fake_write(failures)
    _check_cache_zero_prompt_no_divide(failures)

    _check_tokens_empty_when_none(failures)
    _check_tokens_empty_when_empty_dict(failures)
    _check_tokens_renders_prompt_and_completion(failures)
    _check_tokens_only_prompt_no_completion(failures)
    _check_tokens_only_completion_no_prompt(failures)
    _check_tokens_empty_when_both_zero(failures)

    _check_thinking_empty_when_none(failures)
    _check_thinking_empty_when_zero_thoughts(failures)
    _check_thinking_renders_when_positive(failures)
    _check_thinking_has_space_after_thk(failures)

    _check_api_empty_when_none(failures)
    _check_api_empty_when_empty_dict(failures)
    _check_api_empty_when_zero_requests(failures)
    _check_api_renders_requests_only(failures)
    _check_api_renders_errors(failures)
    _check_api_renders_latency(failures)
    _check_api_renders_all_fields(failures)

    _check_files_empty_when_none(failures)
    _check_files_empty_when_empty_dict(failures)
    _check_files_empty_when_both_zero(failures)
    _check_files_renders_added(failures)
    _check_files_renders_removed(failures)
    _check_files_renders_added_and_removed(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: qwen.py formatters - cache/tokens/thinking/api-stats/files all render"
        " correctly and guard branches return empty strings as specified"
    )


if __name__ == "__main__":
    main()
