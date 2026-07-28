"""Wire status-line settings for Claude, Codex, Qwen, Kimi, Antigravity, and Pi.

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
  --platform codex        Installs a native preset to ~/.codex/config.toml
  --platform kimi         Installs [status_line] command to ~/.kimi-code/tui.toml

Usage (typically via the install.sh / install.bat wrappers):
    python install.py --repo /abs/path/to/repo [--platform claude|codex|qwen|both|antigravity|pi|kimi] [--dry-run]
"""

import argparse
import json
import os
import sys

from statusline_lib.claude_family_install import (
    desired_statusline_entries,
    merge_statusline_family_settings,
    missing_required_scripts,
    statusline_family_already_current,
)
from statusline_lib.codex_install import codex_config_current, merge_codex_config
from statusline_lib.kimi_install import kimi_config_current, merge_kimi_config
from statusline_lib.nudge_install import _nudge_command, _nudge_markers
from statusline_lib.platform_commands import (
    STATUSLINE_REFRESH_SECONDS,
    _commands_for_platform,
    _kimi_command_for_platform,
    _kimi_space_path_warning,
    _pi_loader_contents,
    _pi_loader_path,
    _qwen_command_for_platform,
)
from statusline_lib.qwen_install import _merge_qwen_statusline, _qwen_settings_current
from statusline_lib.settings_io import atomic_write_settings, load_settings


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
        choices=["claude", "codex", "qwen", "both", "antigravity", "pi", "kimi"],
        default="claude",
        help="Which CLI to install for (default: claude)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned install changes and exit without writing",
    )
    return parser.parse_args()


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

    if platform in ("codex", "kimi"):
        result = _install_toml_platform(repo, args.dry_run, platform)
        if result != 0:
            return result

    return 0


def _install_toml_platform(repo, dry_run, platform):
    """Shared install flow for the TOML-config platforms: Codex's native
    footer preset and Kimi Code CLI's [status_line] command."""
    if platform == "kimi":
        target, command = _kimi_command_for_platform(repo)
        config_path = os.path.expanduser("~/.kimi-code/tui.toml")
        is_current = lambda text: kimi_config_current(text, command)
        merge = lambda text: merge_kimi_config(text, command)
        detail = f"  status_line.command: {command}"
        session_label = "Kimi Code"
    else:
        target = None
        config_path = os.path.expanduser("~/.codex/config.toml")
        is_current = codex_config_current
        merge = merge_codex_config
        detail = "  tui.status_line:    native Codex preset"
        session_label = "Codex CLI"
    if platform == "kimi" and (warning := _kimi_space_path_warning(target)):
        print(warning, file=sys.stderr)
    if target is not None and not os.path.exists(target):
        print(f"error: expected file not found: {target}", file=sys.stderr)
        print("  (is --repo pointing at a complete checkout?)", file=sys.stderr)
        return 1
    try:
        current = _load_text(config_path)
    except OSError as exc:
        print(f"error: could not read {config_path}: {exc}", file=sys.stderr)
        return 1
    try:
        if current is not None and is_current(current):
            if dry_run:
                print(f"# {config_path} already current -- nothing to write")
            else:
                print(f"already current: {config_path}\n{detail}\nNothing to do.")
            return 0
        merged = merge(current or "")
    except ValueError as exc:
        print(
            f"error: could not merge {config_path}: {exc}\n"
            "  refusing to overwrite the existing config -- fix it first",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print(f"# would write to {config_path}")
        print(merged, end="")
        return 0
    _atomic_write_text(config_path, merged)
    print(f"updated {config_path}\n{detail}")
    print(f"Open a new {session_label} session to pick it up.")
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


def _install_claude_family(
    repo, dry_run, platform, settings_path, session_label, on_installed=None
):
    """Install statusLine + subagentStatusLine + nudge hook for a
    Claude-settings.json-shaped platform (Claude Code or Antigravity CLI --
    see statusline_lib.claude_family_install for why the merge logic itself
    lives there instead of here)."""
    main_target, subagent_target, main_command, subagent_command = (
        _commands_for_platform(repo, platform=platform)
    )
    nudge_target, nudge_command = _nudge_command(repo, platform=platform)
    nudge_markers = _nudge_markers(nudge_target)

    missing = missing_required_scripts(main_target, subagent_target, nudge_target)
    if missing:
        print(f"error: expected file not found: {missing[0]}", file=sys.stderr)
        print("  (is --repo pointing at a complete checkout?)", file=sys.stderr)
        return 1

    try:
        settings = load_settings(settings_path)
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

    desired_statusline, desired_subagent = desired_statusline_entries(
        main_command, subagent_command, STATUSLINE_REFRESH_SECONDS
    )
    already_current = statusline_family_already_current(
        settings, desired_statusline, desired_subagent, nudge_markers, nudge_command
    )

    if already_current:
        if dry_run:
            print(f"# {settings_path} already current -- nothing to write")
        else:
            print(f"already current: {settings_path}")
            print(
                f"  statusLine:         {main_command}  (refresh {STATUSLINE_REFRESH_SECONDS}s)"
            )
            print(f"  subagentStatusLine: {subagent_command}")
            print(f"  UserPromptSubmit:   {nudge_command}")
            print("Nothing to do.")
        return 0

    merge_statusline_family_settings(
        settings, desired_statusline, desired_subagent, nudge_markers, nudge_command
    )

    if dry_run:
        print(f"# would write to {settings_path}")
        print(json.dumps(settings, indent=2))
        return 0

    atomic_write_settings(settings_path, settings)
    print(f"updated {settings_path}")
    print(
        f"  statusLine:         {main_command}  (refresh {STATUSLINE_REFRESH_SECONDS}s)"
    )
    print(f"  subagentStatusLine: {subagent_command}")
    print(f"  UserPromptSubmit:   {nudge_command}")

    if on_installed is not None:
        on_installed()

    print(f"Open a new {session_label} session (or trigger a render) to pick it up.")
    return 0


def _install_claude(repo, dry_run):
    """Install statusLine + subagentStatusLine + nudge hook for Claude Code."""
    return _install_claude_family(
        repo,
        dry_run,
        platform="claude",
        settings_path=os.path.expanduser("~/.claude/settings.json"),
        session_label="Claude Code",
        on_installed=_report_walker,
    )


def _install_qwen(repo, dry_run):
    """Install ui.statusLine for Qwen Code."""
    settings_path = os.path.expanduser("~/.qwen/settings.json")

    qwen_target, qwen_command = _qwen_command_for_platform(repo)

    if not os.path.exists(qwen_target):
        print(f"error: expected file not found: {qwen_target}", file=sys.stderr)
        print("  (is --repo pointing at a complete checkout?)", file=sys.stderr)
        return 1

    try:
        settings = load_settings(settings_path)
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

    atomic_write_settings(settings_path, settings)
    print(f"updated {settings_path}")
    print(f"  ui.statusLine:      {qwen_command}")

    print("Open a new Qwen Code session (or trigger a render) to pick it up.")
    return 0


def _install_antigravity(repo, dry_run):
    """Install statusLine + subagentStatusLine + nudge hook for Antigravity CLI."""
    return _install_claude_family(
        repo,
        dry_run,
        platform="antigravity",
        settings_path=os.path.expanduser("~/.gemini/antigravity-cli/settings.json"),
        session_label="Antigravity CLI",
    )


if __name__ == "__main__":
    sys.exit(main())
