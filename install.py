"""Wire statusLine + subagentStatusLine into ~/.claude/settings.json.

Idempotent: re-running just refreshes the two `command` strings; every other
key in settings.json is preserved verbatim. If both entries already match what
we'd write, it reports "already current" and exits without touching the file.
Both entries are written together
because the two scripts are paired -- the lead and per-agent renderings share
formatting code, so installing one without the other gives a mismatched UI.

Usage (typically via the install.sh / install.bat wrappers):
    python install.py --repo /abs/path/to/repo [--settings PATH] [--dry-run]
"""

import argparse
import json
import os
import sys


def _load(path):
    """Return parsed dict from `path`, {} if missing/empty, or raise."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} is not a JSON object (top-level type: {type(data).__name__})"
        )
    return data


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute path to the schoen-claude-status checkout",
    )
    parser.add_argument(
        "--settings",
        default=None,
        help="Path to settings.json (default ~/.claude/settings.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the merged JSON and exit without writing",
    )
    args = parser.parse_args()

    # Forward slashes -- bash on Windows (Git Bash, MSYS) handles them and the
    # JSON value stays readable across platforms.
    repo = os.path.abspath(args.repo).replace("\\", "/")
    settings_path = args.settings or os.path.expanduser("~/.claude/settings.json")

    # On Windows, bare python/python3 resolve to the Microsoft Store alias shim,
    # whose ~750ms per-invocation launch overhead dominated every render. Invoke
    # the python.org build via the `py` launcher directly, skipping BOTH the
    # Store shim AND the bash wrapper -- ~50-90ms faster and far less jittery
    # than `bash statusline-command.sh` (Claude Code wraps the command in
    # `cmd /c` on Windows, so no shell prefix is needed). `py -3` keeps it
    # robust across Python minor upgrades -- no hard-coded interpreter path.
    # On other platforms bash + python3 are already fast, so keep the portable
    # shim (which itself prefers `py` where present -- see statusline-command.sh).
    if os.name == "nt":
        main_target = f"{repo}/statusline.py"
        subagent_target = f"{repo}/subagent_statusline.py"
        main_command = f'py -3 "{main_target}"'
        subagent_command = f'py -3 "{subagent_target}"'
    else:
        main_target = f"{repo}/statusline-command.sh"
        subagent_target = f"{repo}/subagent-statusline.sh"
        main_command = f'bash "{main_target}"'
        subagent_command = f'bash "{subagent_target}"'

    for script in (main_target, subagent_target):
        if not os.path.exists(script):
            print(f"error: expected file not found: {script}", file=sys.stderr)
            print("  (is --repo pointing at a complete checkout?)", file=sys.stderr)
            return 1

    try:
        settings = _load(settings_path)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"error: could not parse {settings_path}: {e}", file=sys.stderr)
        print(
            "  refusing to overwrite a malformed settings file -- fix or move it first",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"error: could not read {settings_path}: {e}", file=sys.stderr)
        return 1

    desired_statusline = {"type": "command", "command": main_command}
    desired_subagent = {"type": "command", "command": subagent_command}

    already_current = (
        settings.get("statusLine") == desired_statusline
        and settings.get("subagentStatusLine") == desired_subagent
    )

    if already_current:
        if args.dry_run:
            print(f"# {settings_path} already current -- nothing to write")
        else:
            print(f"already current: {settings_path}")
            print(f"  statusLine:         {main_command}")
            print(f"  subagentStatusLine: {subagent_command}")
            print("Nothing to do.")
        return 0

    settings["statusLine"] = desired_statusline
    settings["subagentStatusLine"] = desired_subagent

    if args.dry_run:
        print(f"# would write to {settings_path}")
        print(json.dumps(settings, indent=2))
        return 0

    _atomic_write(settings_path, settings)
    print(f"updated {settings_path}")
    print(f"  statusLine:         {main_command}")
    print(f"  subagentStatusLine: {subagent_command}")

    # Optional native pace-walker (claude-walker). Pure speedup -- the Python
    # fallback runs identically when it isn't found.
    sys.path.insert(0, repo)
    try:
        from statusline_lib import _find_walker_binary

        walker = _find_walker_binary()
    except ImportError:
        walker = None
    if walker:
        print(f"  walker (native):    {walker}")
    else:
        print("  walker (native):    not found -- using Python fallback")
        print(
            "                      build ~/claude-walker/cpp or set CLAUDE_WALKER_BIN to enable"
        )

    print("Open a new Claude Code session (or trigger a render) to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
