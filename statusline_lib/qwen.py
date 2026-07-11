"""Qwen Code metric formatters for the Qwen Code statusline port.

Split out of cost.py: these work with Qwen Code's
metrics.models.<id>.tokens and .api structures, a separate concern from the
Claude Code cost/cache walking that dominates cost.py.

Payload structure: {prompt, completion, total, cached, thoughts}
- `prompt` = total prompt tokens (includes cached reads)
- `cached` = cache_read_input_tokens (subset of prompt)
- `completion` = output tokens
- `thoughts` = reasoning/thinking tokens

Cache renders as `cached / hit%` -- unlike Claude Code, Qwen has no priced
cache-write side, so no write figure is shown (see format_qwen_cache).
Thinking tokens are appended to the context column as (thk NNNK).
"""

from .base import (
    CTX_DENOM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    fmt,
)
from .cachefmt import format_cache_hit, format_cache_read


def format_qwen_cache(cached, prompt):
    """Format cache as `cached / hit%` -- no Claude-style write figure.

    Qwen's implicit prompt cache bills cache-hit tokens at ~20% of the
    standard input rate (Alibaba Model Studio docs: "the portion of the input
    served from the cache is billed at 20% of the standard input token
    price"), so cached tokens are a real, priced cost signal. But Qwen has no
    priced cache-*write* side -- unlike Claude, there's no premium for
    populating the cache, so a write figure here would borrow Claude's
    write-cost visual language (CACHE_WRITE color) for a number that costs
    nothing. Hit rate = cached / prompt.
    Returns: e.g. `730.0K / 73%` or "" if no cached data.
    """
    if not cached or cached <= 0:
        return ""
    return f"{format_cache_read(cached)} / {format_cache_hit(cached, prompt)}"


def format_qwen_tokens(tokens):
    """Format Qwen Code token metrics as plain arrows (no emojis).

    Input: {"prompt": N, "completion": N, "total": N, "cached": N, "thoughts": N}
    Returns: colored string like "↑2.44M ↓35.2K" or "" if empty.
    Matches Claude Code statusline: ↑ for input, ↓ for output.
    """
    if not tokens:
        return ""
    prompt = int(tokens.get("prompt") or 0)
    completion = int(tokens.get("completion") or 0)

    parts = []
    if prompt:
        parts.append(f"↑{GREEN}{fmt(prompt)}{RESET}")
    if completion:
        parts.append(f"↓{YELLOW}{fmt(completion)}{RESET}")
    return " ".join(parts) if parts else ""


def format_qwen_thinking(tokens):
    """Extract thinking tokens from Qwen metrics for the context column.

    Input: {"prompt": N, "completion": N, "total": N, "cached": N, "thoughts": N}
    Returns: colored string like "(thk 10.1K)" or "" if no thinking tokens.
    """
    if not tokens:
        return ""
    thoughts = int(tokens.get("thoughts") or 0)
    if thoughts <= 0:
        return ""
    return f"{CTX_DENOM}(thk {fmt(thoughts)}){RESET}"


def format_qwen_api_stats(api):
    """Format Qwen Code API stats: requests, errors, latency.

    Input: {"total_requests": N, "total_errors": N, "total_latency_ms": N}
    Returns: colored string like "10req 0err 5.0s" or "" if empty.
    """
    if not api:
        return ""
    requests = int(api.get("total_requests") or 0)
    errors = int(api.get("total_errors") or 0)
    latency_ms = int(api.get("total_latency_ms") or 0)

    if not requests:
        return ""

    parts = [f"{requests}req"]
    if errors:
        parts.append(f"{RED}{errors}err{RESET}")
    if latency_ms:
        latency_s = latency_ms / 1000.0
        parts.append(f"{latency_s:.1f}s")
    return " ".join(parts)


def format_qwen_files(files):
    """Format Qwen Code file change stats: lines added/removed.

    Input: {"total_lines_added": N, "total_lines_removed": N}
    Returns: colored string like "+120/-30" or "" if no changes.
    """
    if not files:
        return ""
    added = int(files.get("total_lines_added") or 0)
    removed = int(files.get("total_lines_removed") or 0)

    if not added and not removed:
        return ""

    parts = []
    if added:
        parts.append(f"{GREEN}+{added}{RESET}")
    if removed:
        parts.append(f"{RED}-{removed}{RESET}")
    return "/".join(parts)
