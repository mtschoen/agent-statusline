"""Base constants and helpers used by all other statusline_lib modules.

No imports from sibling modules — keeps the dependency order clean.
"""

import json
import os

# orjson: optional, ~3-5x faster per-line parse; stdlib json fallback.
try:
    from orjson import loads as _json_loads
except ImportError:
    _json_loads = json.loads


def _truecolor(r, g, b):
    return f"\x1b[38;2;{r};{g};{b}m"


# Threshold band on one brightness plane (175/#af) so ramped and solid colors
# never differ in vividness; truecolor matches the ramp endpoints exactly.
GREEN = _truecolor(0, 175, 0)  # #00af00
YELLOW = _truecolor(175, 175, 0)  # #afaf00, olive
ORANGE = _truecolor(175, 90, 0)  # #af5a00, between yellow and red
RED = _truecolor(175, 0, 0)  # #af0000
RESET = "\x1b[0m"
# Identity colors (256-color) -- distinct from the threshold band so identity
# never reads as a warning.
CACHE_READ = "\x1b[38;5;38m"  # teal
CACHE_WRITE = ORANGE  # cache-write identity reuses the orange hue
CTX_DENOM = "\x1b[38;5;139m"  # soft mauve
# Full-breakdown identity hues for the two non-cache cost components, kept
# distinct from the teal/orange cache pair and the mauve denom so all four
# figures read apart at a glance.
INPUT_TOK = "\x1b[38;5;67m"  # steel blue -- fresh (full-price) input tokens
OUTPUT_TOK = "\x1b[38;5;141m"  # light violet -- generated output tokens


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}K"
    return str(int(n))


def color_high_bad(pct, warn, danger, decimals=0):
    """Higher is worse (e.g. quota %, day %). Smooth gradient: solid green at/below
    warn, ramps through yellow to red by danger (pass warn < danger)."""
    spec = f".{decimals}f"
    return f"{ramp_color_for(pct, warn, danger)}{format(pct, spec)}%{RESET}"


def color_high_good(pct, warn, danger, decimals=0):
    """Higher is better (e.g. cache hit %). Smooth gradient: solid green at/above
    warn, ramps through yellow to red by danger (pass warn > danger, e.g. 90, 75)."""
    spec = f".{decimals}f"
    return f"{ramp_color_for(pct, warn, danger)}{format(pct, spec)}%{RESET}"


# Green -> yellow -> red ramp anchors (RGB); shared by burn rate, quota %,
# cache-hit %, pace deltas. Same 175/#af plane as the solid band above.
RAMP = [(0, 175, 0), (175, 175, 0), (175, 0, 0)]


def ramp_color(t):
    """Truecolor escape on the green(0)->yellow->red(1) ramp for t, clamped to
    [0,1]; piecewise-linear between the RAMP anchors."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    position = t * (len(RAMP) - 1)
    index = min(int(position), len(RAMP) - 2)
    fraction = position - index
    (r0, g0, b0), (r1, g1, b1) = RAMP[index], RAMP[index + 1]
    r = round(r0 + (r1 - r0) * fraction)
    g = round(g0 + (g1 - g0) * fraction)
    b = round(b0 + (b1 - b0) * fraction)
    return f"\x1b[38;2;{r};{g};{b}m"


def ramp_color_for(value, warn, danger):
    """ramp_color for a threshold-style value: `warn` is the green edge, `danger`
    the red edge. Solid green at/beyond warn (away from danger), ramps through
    yellow to red at danger, solid red beyond. Covers high-bad (warn < danger)
    and high-good (warn > danger) by orientation of the two anchors."""
    if warn == danger:
        return ramp_color(1.0 if value >= warn else 0.0)
    return ramp_color((value - warn) / (danger - warn))


def app_dir():
    """Return the absolute path to the app's configuration/data directory.
    Defaults to ~/.claude, but switches to ~/.gemini/antigravity-cli
    if running under Antigravity CLI."""
    if os.environ.get("STATUSLINE_PLATFORM") == "antigravity":
        return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
    if os.environ.get("STATUSLINE_PLATFORM") == "claude":
        return os.path.join(os.path.expanduser("~"), ".claude")

    if os.environ.get("ANTIGRAVITY_AGENT") == "1" or os.environ.get(
        "ANTIGRAVITY_CONVERSATION_ID"
    ):
        home = os.path.expanduser("~")
        # Test isolation check: if home is mocked in verify tests, .gemini/antigravity-cli won't exist
        # but .claude might.
        if not os.path.exists(
            os.path.join(home, ".gemini", "antigravity-cli")
        ) and os.path.exists(os.path.join(home, ".claude")):
            return os.path.join(home, ".claude")
        return os.path.join(home, ".gemini", "antigravity-cli")
    return os.path.join(os.path.expanduser("~"), ".claude")
