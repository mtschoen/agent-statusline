#!/usr/bin/env python3
"""statusline-ctl: live control of the schoen-claude-status status line.

Reads/writes ~/.claude/.statusline-prefs.json (the file the status line reads
fresh every render), so toggles take effect with NO Claude Code restart. This is
the source of truth; the /statusline skill is a thin natural-language wrapper
over this CLI.

Usage:
  statusline-ctl list                 show every setting + its effective value
  statusline-ctl get   <key>          show one setting
  statusline-ctl set   <key> <value>  write a live override
  statusline-ctl reset <key>          drop the override (fall back to env/default)
  statusline-ctl path                 print the prefs file path

Keys (friendly name -> what it controls):
  cost          on|off              show or hide every $ figure on line 2
  compact       off|auto|always     line-2 width shrinking
  target-rate   <$/min>|auto|off    the ->$ target: pin a number, derive it
                                     (weekly-sustainable for subs), or disable
  daily-budget  <$/day>|off         API-key daily budget (sets the needle)
  verbose-pace  on|off              numeric pace deltas instead of the glyph
  beacon        on|off              render the progress-beacon ETA column

Precedence the status line applies: prefs file (this CLI) > settings.json env >
built-in default. `reset` removes the prefs entry so the env/default shows again.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from statusline_lib.prefs import load_prefs, pref, prefs_path


class _Setting:
    """One friendly key: the STATUSLINE_* var it writes, a normalizer that turns
    user input into a (stored_string, error) pair -- error is None on success,
    or a message string on bad input -- and an accepted-values blurb for help."""

    def __init__(self, env, normalize, values):
        self.env = env
        self.normalize = normalize
        self.values = values


def _onoff(true_val, false_val):
    """A normalizer for a two-state friendly toggle -> (stored string, error)."""

    def norm(raw):
        v = raw.strip().lower()
        if v in ("on", "true", "1", "yes"):
            return true_val, None
        if v in ("off", "false", "0", "no"):
            return false_val, None
        return None, "expected on|off"

    return norm


def _norm_compact(raw):
    v = raw.strip().lower()
    if v in ("off", "auto", "always"):
        return v, None
    return None, "expected off|auto|always"


def _parse_positive(raw, units):
    """Shared $/min and $/day parse: a positive float, returned as (repr, None),
    else (None, message)."""
    try:
        value = float(raw)
    except ValueError:
        return None, f"expected a {units} number or 'off'"
    if value <= 0:
        return None, "must be > 0 (use 'off' to disable)"
    return repr(value), None


def _norm_target_rate(raw):
    v = raw.strip().lower()
    if v == "auto":
        return "auto", None
    if v in ("off", "none"):
        return "0", None  # 0 disables the arrow + coloring in _resolve_target_rate
    return _parse_positive(v, "$/min")


def _norm_daily_budget(raw):
    v = raw.strip().lower()
    if v in ("off", "none"):
        return "0", None  # 0/<=0 -> None (disabled) in _daily_budget
    return _parse_positive(v, "$/day")


# cost on = SHOW => HIDE_COST "0"; cost off = HIDE => HIDE_COST "1".
SETTINGS = {
    "cost": _Setting("STATUSLINE_HIDE_COST", _onoff("0", "1"), "on|off"),
    "compact": _Setting("STATUSLINE_COMPACT", _norm_compact, "off|auto|always"),
    "target-rate": _Setting(
        "STATUSLINE_TARGET_RATE", _norm_target_rate, "<$/min>|auto|off"
    ),
    "daily-budget": _Setting(
        "STATUSLINE_DAILY_BUDGET", _norm_daily_budget, "<$/day>|off"
    ),
    "verbose-pace": _Setting("STATUSLINE_VERBOSE_PACE", _onoff("1", "0"), "on|off"),
    "beacon": _Setting("STATUSLINE_BEACON", _onoff("1", "0"), "on|off"),
}


def _write_prefs(data):
    """Atomically write the prefs dict (temp file + os.replace)."""
    path = prefs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _effective(setting):
    """The status line's resolved string for one setting (prefs > env > unset)."""
    return pref(setting.env)


def _origin(setting, prefs):
    """Where the effective value comes from: 'prefs', 'env', or 'default'."""
    if setting.env in prefs and prefs[setting.env] is not None:
        return "prefs"
    if os.environ.get(setting.env) is not None:
        return "env"
    return "default"


def _cmd_list(_args):
    prefs = load_prefs()
    width = max(len(k) for k in SETTINGS)
    print(f"prefs file: {prefs_path()}")
    for key, setting in SETTINGS.items():
        value = _effective(setting)
        shown = value if value is not None else "(unset)"
        print(
            f"  {key.ljust(width)}  {shown:<10}  [{_origin(setting, prefs)}]"
            f"  {setting.env} = {setting.values}"
        )
    return 0


def _cmd_get(args):
    if len(args) != 1 or args[0] not in SETTINGS:
        return _usage_error("get <key>")
    setting = SETTINGS[args[0]]
    value = _effective(setting)
    print(value if value is not None else "(unset)")
    return 0


def _cmd_set(args):
    if len(args) != 2 or args[0] not in SETTINGS:
        return _usage_error("set <key> <value>")
    key, raw = args
    setting = SETTINGS[key]
    stored, err = setting.normalize(raw)
    if err is not None:
        print(f"error: {key}: {err}", file=sys.stderr)
        return 2
    prefs = load_prefs()
    prefs[setting.env] = stored
    _write_prefs(prefs)
    print(f"{key} = {stored}  (effective now; no restart needed)")
    return 0


def _cmd_reset(args):
    if len(args) != 1 or args[0] not in SETTINGS:
        return _usage_error("reset <key>")
    setting = SETTINGS[args[0]]
    prefs = load_prefs()
    if setting.env in prefs:
        del prefs[setting.env]
        _write_prefs(prefs)
    print(f"{args[0]} reset (now: {_effective(setting) or '(unset -> default)'})")
    return 0


def _cmd_path(_args):
    print(prefs_path())
    return 0


def _usage_error(form):
    keys = ", ".join(SETTINGS)
    print(f"usage: statusline-ctl {form}\nkeys: {keys}", file=sys.stderr)
    return 2


_COMMANDS = {
    "list": _cmd_list,
    "get": _cmd_get,
    "set": _cmd_set,
    "reset": _cmd_reset,
    "path": _cmd_path,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command = _COMMANDS.get(argv[0])
    if command is None:
        print(f"error: unknown command {argv[0]!r}", file=sys.stderr)
        return _usage_error("<list|get|set|reset|path> ...")
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
