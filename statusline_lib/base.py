"""Base constants and helpers used by all other statusline_lib modules.

No imports from sibling modules — keeps the dependency order clean.
"""

import json
import os
import socket
import sys
import time

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
    and high-good (warn > danger) by orientation of the two anchors.

    warn == danger collapses both anchors onto one point, which carries no
    orientation: nothing distinguishes a high-bad caller from a high-good one,
    so favoring either color extreme would invert intent for the other. Render
    the neutral ramp midpoint instead of guessing."""
    if warn == danger:
        return ramp_color(0.5)
    return ramp_color((value - warn) / (danger - warn))


def _platform_from_argv():
    """Return the `--statusline-platform <value>` (or `=`-joined) argv value,
    or None. install.py injects this into the configured command string for
    Antigravity CLI so platform routing is deterministic regardless of
    whether the host CLI sets ANTIGRAVITY_AGENT / ANTIGRAVITY_CONVERSATION_ID
    for the statusline subprocess -- it does not, which was the root cause of
    Antigravity's statusline state/logs landing in ~/.claude instead of
    ~/.gemini/antigravity-cli."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--statusline-platform" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--statusline-platform="):
            return arg.split("=", 1)[1]
    return None


def app_dir():
    """Return the absolute path to the app's configuration/data directory.
    Defaults to ~/.claude, but switches to ~/.gemini/antigravity-cli
    if running under Antigravity CLI."""
    platform = os.environ.get("STATUSLINE_PLATFORM") or _platform_from_argv()
    if platform == "antigravity":
        return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
    if platform == "claude":
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


def state_dir(state_dir=None):
    """Resolve the state directory: explicit arg > CLAUDE_STATE_DIR >
    ANTIGRAVITY_STATE_DIR > app_dir()/state. Shared by every module that
    persists per-session or per-key state on disk (wrap-nudge occupancy,
    render-timer, the git-ref/beacons-latest TTL caches) -- the two env vars
    exist purely for verify-script test isolation so tests never touch the
    real ~/.claude/state."""
    return (
        state_dir
        or os.environ.get("CLAUDE_STATE_DIR")
        or os.environ.get("ANTIGRAVITY_STATE_DIR")
        or os.path.join(app_dir(), "state")
    )


def sanitize_state_key(key):
    """Keep a state-file key (session id, etc.) filename-safe. Session ids
    are UUID-ish in practice, but a path component should never be built
    from unsanitized input."""
    return "".join(c for c in str(key or "") if c.isalnum() or c in "-_")


# --- Entry-script glue shared by statusline.py / qwen_statusline.py /
# subagent_statusline.py. Small enough, and used narrowly enough (best-effort
# I/O helpers, host/mode/spinner lookups), that a dedicated module would be
# more ceremony than the code it holds.

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner_frame():
    """The spinner glyph for "now", cycling through SPINNER_FRAMES at 4Hz."""
    return SPINNER_FRAMES[int(time.time() * 4) % len(SPINNER_FRAMES)]


def hostname():
    """Short hostname (no domain suffix), or "unknown" if unavailable."""
    try:
        return socket.gethostname().split(".")[0] or "unknown"
    except OSError:
        return "unknown"


def is_local_mode():
    """True when the render should badge itself LOCAL: either local-mode env
    var is set, or the ~/.claude/.local-mode marker file exists."""
    return (
        os.environ.get("CLAUDE_LOCAL_MODE") == "1"
        or os.environ.get("ANTIGRAVITY_LOCAL_MODE") == "1"
        or os.path.isfile(os.path.join(app_dir(), ".local-mode"))
    )


def safe_write(path, text):
    """Best-effort whole-file write (debug/input-log dumps). A failed write
    is non-fatal and must never break rendering."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        # Best-effort write (cache/state/log file); a failed write is
        # non-fatal and must not break rendering.
        pass


def log_traceback(path):
    """Best-effort append a timestamped traceback of the in-flight exception
    to `path` (call from within an `except` block). The logger itself must
    never raise: an unwritable log path costs the trace, not the caller's own
    error handling."""
    try:
        import traceback

        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exc(file=f)
    except OSError:
        # The logger itself must never raise; if the log file is unwritable
        # there is nothing useful left to do.
        pass
