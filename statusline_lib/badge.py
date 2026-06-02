"""Context window and model-badge rendering helpers.

Pure formatting - no transcript I/O or cost calculation.

Imports:
  base -- for color constants, fmt
"""

import contextlib
import os
import re as _re

from .base import (
    CTX_DENOM,
    GREEN,
    ORANGE,
    RED,
    RESET,
    YELLOW,
    fmt,
)

COMPACT_BUFFER_TOKENS = 33_000
RED_MARGIN_TOKENS = 20_000
ORANGE_THRESHOLD_1M_TOKENS = 500_000  # mid-band warning for 1M-context sessions


def ctx_window_for_model(model_id):
    """Best-effort window inference for per-agent rendering. Opus [1m] -> 1M,
    everything else -> 200K. The main script doesn't need this -- the payload
    carries `context_window.context_window_size` directly."""
    return 1_000_000 if "[1m]" in (model_id or "") else 200_000


def format_context(ctx_used, window_size, model_id="", show_denom=True, show_pct=True):
    """`usedK / windowK (P.P%)` colored by token-anchored thresholds.

    Yellow at 200K for 1M models (Opus 1M pricing boundary), at 50% otherwise.
    1M models also get an orange mid-band at 500K so the huge yellow
    span between the pricing boundary and auto-compact has a visible
    midpoint cue. Red at `window_size - 33K compact buffer - 20K
    headroom`; tracks CLAUDE_AUTOCOMPACT_PCT_OVERRIDE if set.

    `show_denom=False` (super-minimal compact) drops the ` / windowK` window
    size; `show_pct=False` drops the trailing `(P.P%)`. The colored used-token
    count always stays.
    """
    if window_size <= 0:
        return ""
    override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    compact_tokens = max(0, window_size - COMPACT_BUFFER_TOKENS)
    if override:
        with contextlib.suppress(ValueError):
            compact_tokens = int(window_size * float(override) / 100)
    red_tokens = max(0, compact_tokens - RED_MARGIN_TOKENS)
    is_1m = window_size >= 1_000_000 or "[1m]" in (model_id or "")
    yellow_tokens = 200_000 if is_1m else window_size // 2
    if ctx_used >= red_tokens:
        ctx_color = RED
    elif is_1m and ctx_used >= ORANGE_THRESHOLD_1M_TOKENS:
        ctx_color = ORANGE
    elif ctx_used >= yellow_tokens:
        ctx_color = YELLOW
    else:
        ctx_color = GREEN
    text = f"{ctx_color}{fmt(ctx_used)}{RESET}"
    if show_denom:
        text += f" / {CTX_DENOM}{fmt(window_size)}{RESET}"
    if show_pct:
        text += f" ({ctx_color}{100.0 * ctx_used / window_size:.1f}%{RESET})"
    return text


# Model-family badge: substring match -> short label + ANSI color. Distinct
# from threshold green/yellow/red and the cache identity teal/orange so a
# coloured badge never reads as a warning or a metric. Shared by the main and
# subagent statuslines.
_MODEL_BADGES = [
    (("opus",), "opus", "\x1b[35m"),  # magenta
    (("sonnet",), "sonnet", "\x1b[36m"),  # cyan
    (("haiku",), "haiku", "\x1b[34m"),  # blue
    # Qwen model families (for Qwen Code port)
    (("qwen-coder", "qwen2.5-coder"), "qwen-coder", "\x1b[96m"),  # bright cyan
    (("qwen",), "qwen", "\x1b[94m"),  # bright blue
]


def _version_for(mid, key):
    """Extract a dotted `major.minor` version following the family `key` in a
    model id, e.g. `claude-opus-4-8` -> "4.8". Returns "" when no version
    component is present (e.g. an aliased id like `opus`).
    """
    match = _re.search(rf"{key}-(\d+)-(\d+)", mid)
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def _qwen_version_for(mid):
    """Extract version from Qwen model names like 'qwen-3-235b' -> '3',
    'qwen2.5-72b' -> '2.5'. Returns "" when no version is found."""
    match = _re.search(r"qwen[-_]?(\d+(?:\.\d+)?)", mid)
    return match.group(1) if match else ""


def _qwen_size_for(mid):
    """Extract parameter size from Qwen model names like 'qwen-3-235b' -> '235b',
    'qwen-3-32b' -> '32b'. Returns "" when no size suffix is found."""
    match = _re.search(r"(\d+[bBmM])(?:[-_]|$)", mid)
    return match.group(1).lower() if match else ""


def format_model_badge(model_id):
    """Colored short model-family badge, e.g. magenta `opus4.8[1m]`.

    Inserts the `major.minor` version when the id carries one and appends the
    `[1m]` runtime-tier suffix when present. Unknown families render as a mauve
    `?`; an empty id returns "" so the caller can omit the segment.

    For Qwen models (e.g. 'qwen-3-235b'), shows version + size like 'qwen3·235b'.
    """
    if not model_id:
        return ""
    mid = model_id.lower()
    suffix = "[1m]" if "[1m]" in mid else ""
    for keys, label, color in _MODEL_BADGES:
        for key in keys:
            if key in mid:
                if key.startswith("qwen"):
                    version = _qwen_version_for(mid)
                    size = _qwen_size_for(mid)
                    size_part = f"·{size}" if size else ""
                    return f"{color}{label}{version}{size_part}{RESET}"
                version = _version_for(mid, key)
                return f"{color}{label}{version}{suffix}{RESET}"
    return f"{CTX_DENOM}?{RESET}"
