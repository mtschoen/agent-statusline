"""Wire statusLine + subagentStatusLine + wrap nudge hook settings for
Claude Code, Qwen Code, Antigravity CLI, and Pi extension loader.

Idempotent: re-running just refreshes each target's install strings; every
other key in settings files is preserved verbatim, and pre-existing extension
loader files are replaced only if needed. If all requested targets already match
what we'd write, it reports "already current" and exits without touching files.

For Claude/Qwen/Antigravity, the two statuslines are written together because
they are paired -- the lead and per-agent renderings share formatting code, so
installing one without the other gives a mismatched UI. The nudge hook is the
consumer of the per-session occupancy file the statusline produces, so it
installs in the same pass for those CLI settings.

Platform support:
  --platform claude       (default) Installs to ~/.claude/settings.json
  --platform qwen         Installs to ~/.qwen/settings.json (ui.statusLine only)
  --platform both         Installs to both Claude and Qwen platforms
  --platform antigravity  Installs to ~/.gemini/antigravity-cli/settings.json
  --platform pi           Installs Pi extension loader at ~/.pi/agent/extensions/agent-statusline/index.ts

Usage (typically via the install.sh / install.bat wrappers):
    python install.py --repo /abs/path/to/repo [--platform claude|qwen|both|antigravity|pi] [--dry-run]
"""

import argparse
import json
import os
import sys

from statusline_lib.nudge_install import (
    _merge_nudge_hook,
    _nudge_command,
    _nudge_hook_current,
    _nudge_markers,
)


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


def _atomic_write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_text(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute path to the schoen-claude-status checkout",
    )
    parser.add_argument(
        "--platform",
        choices=["claude", "qwen", "both", "antigravity", "pi"],
        default="claude",
        help="Which CLI to install for (default: claude)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned install changes and exit without writing",
    )
    return parser.parse_args()


def _commands_for_platform(repo, platform="claude"):
    """Return (main_target, subagent_target, main_command, subagent_command)."""
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
        if platform == "antigravity":
            return (
                main_target,
                subagent_target,
                f"py -3 {main_target}",
                f"py -3 {subagent_target}",
            )
        return (
            main_target,
            subagent_target,
            f'py -3 "{main_target}"',
            f'py -3 "{subagent_target}"',
        )
    main_target = f"{repo}/statusline-command.sh"
    subagent_target = f"{repo}/subagent-statusline.sh"
    return (
        main_target,
        subagent_target,
        f'bash "{main_target}"',
        f'bash "{subagent_target}"',
    )


def _qwen_command_for_platform(repo):
    """Return (target, command) for Qwen Code statusline."""
    # Qwen Code uses the same platform-aware invocation strategy as Claude Code.
    target = f"{repo}/qwen_statusline.py"
    if os.name == "nt":
        command = f'py -3 "{target}"'
    else:
        command = f'bash "{repo}/qwen-statusline-command.sh"'
    return target, command


def _report_walker():
    # Optional native pace-walker (claude-walker). Pure speedup -- the Python
    # fallback runs identically when it isn't found. statusline_lib is already
    # imported (top-level nudge_install import), so no sys.path setup is needed
    # here; the guard only covers _find_walker_binary being absent.
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


def _qwen_settings_current(settings, command):
    """True iff Qwen ui.statusLine already matches `command`."""
    ui = settings.get("ui") or {}
    status_line = ui.get("statusLine") or {}
    return (
        status_line.get("type") == "command" and status_line.get("command") == command
    )


def _merge_qwen_statusline(settings, command):
    """Insert or update ui.statusLine, preserving other ui keys."""
    ui = settings.setdefault("ui", {})
    ui["statusLine"] = {"type": "command", "command": command}


def _pi_loader_path():
    return os.path.expanduser("~/.pi/agent/extensions/agent-statusline/index.ts")


def _pi_loader_contents(repo):
    return f'export {{ default }} from "{repo}/pi-extension/index.ts";\n'


def main():
    args = _parse_args()
    platform = args.platform

    # Forward slashes -- bash on Windows (Git Bash, MSYS) handles them and the
    # JSON value stays readable across platforms.
    repo = os.path.abspath(args.repo).replace("\\", "/")

    install_claude = platform in ("claude", "both")
    install_qwen = platform in ("qwen", "both")
    install_antigravity = platform == "antigravity"
    install_pi = platform == "pi"

    if install_claude:
        result = _install_claude(repo, args.dry_run)
        if result != 0:
            return result

    if install_qwen:
        result = _install_qwen(repo, args.dry_run)
        if result != 0:
            return result

    if install_antigravity:
        result = _install_antigravity(repo, args.dry_run)
        if result != 0:
            return result

    if install_pi:
        result = _install_pi(repo, args.dry_run)
        if result != 0:
            return result

    return 0


def _install_pi(repo, dry_run):
    """Install Pi extension loader that mounts the statusline footer."""
    extension_path = _pi_loader_path()
    source = f"{repo}/pi-extension/index.ts"

    if not os.path.exists(source):
        print(f"error: expected file not found: {source}", file=sys.stderr)
        print("  (is --repo pointing at a complete checkout?)", file=sys.stderr)
        return 1

    desired = _pi_loader_contents(repo)
    if os.path.exists(extension_path) and not os.access(extension_path, os.R_OK):
        print(
            f"error: could not read {extension_path}: permission denied",
            file=sys.stderr,
        )
        return 1

    current = _load_text(extension_path)
    already_current = current is not None and current.strip() == desired.strip()

    if already_current:
        if dry_run:
            print(f"# {extension_path} already current -- nothing to write")
        else:
            print(f"already current: {extension_path}")
            print(f"  loader:             {desired.strip()}")
            print("Nothing to do.")
        return 0

    if dry_run:
        print(f"# would write to {extension_path}")
        print(desired)
        return 0

    _atomic_write_text(extension_path, desired)
    print(f"updated {extension_path}")
    print(f"  loader:             {desired.strip()}")
    print("Open a new Pi session (or restart Pi) to pick it up.")
    return 0


def _install_claude(repo, dry_run):
    """Install statusLine + subagentStatusLine + nudge hook for Claude Code."""
    settings_path = os.path.expanduser("~/.claude/settings.json")

    main_target, subagent_target, main_command, subagent_command = (
        _commands_for_platform(repo, platform="claude")
    )
    nudge_target, nudge_command = _nudge_command(repo, platform="claude")
    nudge_markers = _nudge_markers(nudge_target)

    for script in (main_target, subagent_target, nudge_target):
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
        # Unreadable settings file: report the cause and abort rather than clobber it.
        print(f"error: could not read {settings_path}: {e}", file=sys.stderr)
        return 1

    desired_statusline = {"type": "command", "command": main_command}
    desired_subagent = {"type": "command", "command": subagent_command}

    already_current = (
        settings.get("statusLine") == desired_statusline
        and settings.get("subagentStatusLine") == desired_subagent
        and _nudge_hook_current(settings, nudge_markers, nudge_command)
    )

    if already_current:
        if dry_run:
            print(f"# {settings_path} already current -- nothing to write")
        else:
            print(f"already current: {settings_path}")
            print(f"  statusLine:         {main_command}")
            print(f"  subagentStatusLine: {subagent_command}")
            print(f"  UserPromptSubmit:   {nudge_command}")
            print("Nothing to do.")
        return 0

    settings["statusLine"] = desired_statusline
    settings["subagentStatusLine"] = desired_subagent
    _merge_nudge_hook(settings, nudge_markers, nudge_command)

    if dry_run:
        print(f"# would write to {settings_path}")
        print(json.dumps(settings, indent=2))
        return 0

    _atomic_write(settings_path, settings)
    print(f"updated {settings_path}")
    print(f"  statusLine:         {main_command}")
    print(f"  subagentStatusLine: {subagent_command}")
    print(f"  UserPromptSubmit:   {nudge_command}")

    _report_walker()

    print("Open a new Claude Code session (or trigger a render) to pick it up.")
    return 0


def _install_qwen(repo, dry_run):
    """Install ui.statusLine for Qwen Code."""
    settings_path = os.path.expanduser("~/.qwen/settings.json")

    qwen_target, qwen_command = _qwen_command_for_platform(repo)

    if not os.path.exists(qwen_target):
        print(f"error: expected file not found: {qwen_target}", file=sys.stderr)
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
        # Unreadable settings file: report the cause and abort rather than clobber it.
        print(f"error: could not read {settings_path}: {e}", file=sys.stderr)
        return 1

    already_current = _qwen_settings_current(settings, qwen_command)

    if already_current:
        if dry_run:
            print(f"# {settings_path} already current -- nothing to write")
        else:
            print(f"already current: {settings_path}")
            print(f"  ui.statusLine:      {qwen_command}")
            print("Nothing to do.")
        return 0

    _merge_qwen_statusline(settings, qwen_command)

    if dry_run:
        print(f"# would write to {settings_path}")
        print(json.dumps(settings, indent=2))
        return 0

    _atomic_write(settings_path, settings)
    print(f"updated {settings_path}")
    print(f"  ui.statusLine:      {qwen_command}")

    print("Open a new Qwen Code session (or trigger a render) to pick it up.")
    return 0


def _install_antigravity(repo, dry_run):
    """Install statusLine + subagentStatusLine + nudge hook for Antigravity CLI."""
    settings_path = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")

    main_target, subagent_target, main_command, subagent_command = (
        _commands_for_platform(repo, platform="antigravity")
    )
    nudge_target, nudge_command = _nudge_command(repo, platform="antigravity")
    nudge_markers = _nudge_markers(nudge_target)

    for script in (main_target, subagent_target, nudge_target):
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
        # Unreadable settings file: report the cause and abort rather than clobber it.
        print(f"error: could not read {settings_path}: {e}", file=sys.stderr)
        return 1

    desired_statusline = {"type": "command", "command": main_command}
    desired_subagent = {"type": "command", "command": subagent_command}

    already_current = (
        settings.get("statusLine") == desired_statusline
        and settings.get("subagentStatusLine") == desired_subagent
        and _nudge_hook_current(settings, nudge_markers, nudge_command)
    )

    if already_current:
        if dry_run:
            print(f"# {settings_path} already current -- nothing to write")
        else:
            print(f"already current: {settings_path}")
            print(f"  statusLine:         {main_command}")
            print(f"  subagentStatusLine: {subagent_command}")
            print(f"  UserPromptSubmit:   {nudge_command}")
            print("Nothing to do.")
        return 0

    settings["statusLine"] = desired_statusline
    settings["subagentStatusLine"] = desired_subagent
    _merge_nudge_hook(settings, nudge_markers, nudge_command)

    if dry_run:
        print(f"# would write to {settings_path}")
        print(json.dumps(settings, indent=2))
        return 0

    _atomic_write(settings_path, settings)
    print(f"updated {settings_path}")
    print(f"  statusLine:         {main_command}")
    print(f"  subagentStatusLine: {subagent_command}")
    print(f"  UserPromptSubmit:   {nudge_command}")

    print("Open a new Antigravity CLI session (or trigger a render) to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
