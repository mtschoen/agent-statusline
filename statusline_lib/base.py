"""Base constants and helpers used by all other statusline_lib modules.

No imports from sibling modules — keeps the dependency order clean.
"""

import json

# orjson: optional, ~3-5x faster per-line parse; stdlib json fallback.
try:
    from orjson import loads as _json_loads
except ImportError:
    _json_loads = json.loads

RED = "\x1b[31m"
YELLOW = "\x1b[33m"
ORANGE = "\x1b[38;5;208m"  # mid-tier between yellow and red
GREEN = "\x1b[32m"
RESET = "\x1b[0m"
# Identity colors (256-color) -- distinct from the threshold band so identity
# never reads as a warning.
CACHE_READ = "\x1b[38;5;38m"  # teal
CACHE_WRITE = ORANGE  # cache-write identity reuses the orange hue
CTX_DENOM = "\x1b[38;5;139m"  # soft mauve


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}K"
    return str(int(n))


def color_high_bad(pct, warn, danger, decimals=0):
    """Higher is worse (e.g. ctx %, quota %). >= warn -> yellow, >= danger -> red."""
    c = RED if pct >= danger else YELLOW if pct >= warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"


def color_high_good(pct, warn, danger, decimals=0):
    """Higher is better (e.g. cache hit %). < warn -> yellow, < danger -> red."""
    c = RED if pct < danger else YELLOW if pct < warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"
