"""Compact-mode width resolution for the main statusline's line 2.

Resolves which embellishments are enabled from STATUSLINE_COMPACT and the
$COLUMNS env var (set by Claude Code >= 2.1.153). No transcript I/O: the caller
re-runs its cheap render function at the verbosity this module decides.

Imports: none from sibling modules (leaf module).
"""

import os
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Drop priority for `auto` mode: the first item is shed first when line 2
# overflows $COLUMNS, the rest in turn until it fits. Tunable. The protected
# fields (TTL count, cost, context used-token count, model badge) are simply
# absent from this list, so they are never dropped. The last three are the
# super-minimal tier: the narrowest windows lose the whole live $/min field,
# then the context window size and percentage, leaving the bare used-token
# count and session cost.
DROP_ORDER = [
    "cache_costs",
    "burn_target",
    "cache_hit",
    "quota_pace",
    "ttl_wasted",
    "burn_rate",
    "context_pct",
    "context_denom",
]


def full_flags():
    """All embellishments on (full verbosity)."""
    return dict.fromkeys(DROP_ORDER, True)


def visible_width(text):
    """Rendered column count: ANSI escapes stripped, then len(). Wide glyphs
    (warning/yin-yang/arrows) may undercount by ~1 each; since the threshold is
    soft, undercounting only ever sheds one extra item - acceptable."""
    return len(_ANSI_RE.sub("", text))


def _columns():
    raw = os.environ.get("COLUMNS")
    if not raw:
        return None
    try:
        cols = int(raw)
    except ValueError:
        return None
    return cols if cols > 0 else None


def _mode():
    return (os.environ.get("STATUSLINE_COMPACT") or "auto").strip().lower()


def resolve_flags(render):
    """Return the embellishment-flag dict for line 2.

    `render(flags) -> str` formats line 2 at the given verbosity; it is called
    repeatedly in `auto` mode to measure width as items drop. Modes: `0`/`never`/
    `full`/`off` -> all on; `1`/`always`/`compact`/`on` -> all off; anything else
    (default `auto`) -> drop in DROP_ORDER until visible width <= $COLUMNS, or
    stay full when $COLUMNS is unset.
    """
    mode = _mode()
    if mode in ("0", "never", "full", "off"):
        return full_flags()
    if mode in ("1", "always", "compact", "on"):
        return dict.fromkeys(DROP_ORDER, False)
    flags = full_flags()
    cols = _columns()
    if cols is None:
        return flags
    dropped = 0
    while visible_width(render(flags)) > cols and dropped < len(DROP_ORDER):
        flags[DROP_ORDER[dropped]] = False
        dropped += 1
    return flags
