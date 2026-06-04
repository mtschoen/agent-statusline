"""Live preferences resolver: a small JSON file the status line reads on every
render so toggles (cost, compact, target rate, ...) take effect WITHOUT a Claude
Code restart.

Why this exists: the status line subprocess inherits its environment once, when
Claude Code launches, so editing settings.json's `env` block does nothing until
restart. This file is read fresh each render, so `statusline-ctl` (or the
/statusline skill) can flip a toggle live. Precedence, highest first:

  1. the prefs file (~/.claude/.statusline-prefs.json) -- live overrides
  2. the process environment (settings.json `env`) -- the configured baseline
  3. the call site's own default

Keys are the same STATUSLINE_* names the env vars use, so a call site swaps
`os.environ.get("STATUSLINE_X")` for `pref("STATUSLINE_X")` with no behavior
change when the prefs file is absent. Values are stored as strings (mirroring
env), so existing float()/truthy parsing downstream is untouched.

Imports: stdlib only (leaf module, like base).
"""

import json
import os

_DEFAULT_PREFS_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-prefs.json"
)


def prefs_path():
    """The prefs file path: the STATUSLINE_PREFS_PATH override, else the default
    ~/.claude/.statusline-prefs.json. The override is a test/relocation seam and
    is read from the real environment (not pref(), which would recurse)."""
    return os.environ.get("STATUSLINE_PREFS_PATH") or _DEFAULT_PREFS_PATH


def load_prefs():
    """The prefs dict, read fresh (no cache -- the file is tiny and we want it
    live). Returns {} on any error: missing file, unreadable, malformed JSON, or
    a non-object top level. Never raises -- a broken prefs file must not take
    down a render."""
    try:
        with open(prefs_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pref(name, default=None):
    """Resolve one STATUSLINE_* setting: prefs file, then environment, then
    `default`. Mirrors os.environ.get(name, default) so call sites swap cleanly;
    returns a str (prefs values are coerced) or `default`.

    A prefs key set to JSON null counts as ABSENT (falls through to env/default),
    so a writer can null a key to re-expose the baseline without rewriting the
    whole file."""
    prefs = load_prefs()
    if name in prefs and prefs[name] is not None:
        return str(prefs[name])
    return os.environ.get(name, default)


_TRUTHY = ("1", "true", "on", "yes")
_FALSEY = ("0", "false", "off", "no")


def pref_bool(name, default=False):
    """Resolve a STATUSLINE_* setting to a bool. 1/true/on/yes -> True,
    0/false/off/no -> False (any case); unset or unrecognized -> `default`."""
    raw = pref(name)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSEY:
        return False
    return default
